"""
3_paginator.py
Direct GraphQL pagination using cursor-based requests.
10-100x faster than browser scrolling. Targets 10,000+ tweets.

Usage:
  python 3_paginator.py                             # default: elonmusk, 10000 tweets
  python 3_paginator.py --user barackobama --max 50000
  python 3_paginator.py --user custom --userid 12345678 --max 5000
  python 3_paginator.py --proxy http://user:pass@host:port
"""

import argparse
import asyncio
import json
import aiohttp
from patchright.async_api import async_playwright

from config import (
    USER_DATA_DIR,
    DEFAULT_TARGET,
    DEFAULT_MAX_TWEETS,
    TWEETS_PER_REQUEST,
    REQUEST_DELAY_SECONDS,
    BEARER_TOKEN,
    GRAPHQL_FEATURES,
    KNOWN_USER_IDS,
    log,
    save_json,
    deduplicate,
    clean_tweet,
)


ASYNC_SEMAPHORE = 5
CHECKPOINT_INTERVAL = 1000
MAX_RETRIES = 3
RETRY_DELAY = 5
REQUEST_TIMEOUT = 30

USER_TWEETS_QUERY_ID = "V7H0jBqZf9kK4Y4vX4Y4vQ"
USER_BY_SCREEN_NAME_QUERY_ID = "G3KGOASz96M-Qu0nwmGXNg"

GRAPHQL_BASE = "https://x.com/i/api/graphql"


async def get_cookies_from_session() -> dict:
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=True,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        raw = await context.cookies()
        await context.close()
        return {c["name"]: c["value"] for c in raw}


async def get_ct0_and_auth(cookies: dict) -> tuple[str, str]:
    ct0 = cookies.get("ct0", "")
    auth_token = cookies.get("auth_token", "")
    if not ct0:
        log.warning("No ct0 cookie found — requests may be rate-limited or rejected")
    return ct0, auth_token


async def resolve_user_id(
    session: aiohttp.ClientSession, username: str, cookies: dict, ct0: str
) -> str | None:
    if username.lower() in KNOWN_USER_IDS:
        uid = KNOWN_USER_IDS[username.lower()]
        log.info("Resolved @%s → %s (cached)", username, uid)
        return uid

    variables = {
        "screen_name": username,
        "withSafetyModeUserFields": True,
    }
    features = {k: v for k, v in GRAPHQL_FEATURES.items()}
    params = {
        "variables": json.dumps(variables),
        "features": json.dumps(features),
        "fieldToggles": json.dumps({"withArticleRichContentState": False}),
    }
    url = f"{GRAPHQL_BASE}/{USER_BY_SCREEN_NAME_QUERY_ID}/UserByScreenName"
    headers = build_graphql_headers(cookies, ct0)

    try:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status != 200:
                log.error("UserByScreenName returned %d", resp.status)
                return None
            data = await resp.json()
            uid = data.get("data", {}).get("user", {}).get("result", {}).get("rest_id")
            if uid:
                log.info("Resolved @%s → %s (via API)", username, uid)
                return uid
            log.error("Could not resolve user ID for @%s", username)
            return None
    except Exception as e:
        log.error("Failed to resolve user ID: %s", e)
        return None


def build_graphql_headers(cookies: dict, ct0: str) -> dict:
    return {
        "Authorization": f"Bearer {BEARER_TOKEN}",
        "Content-Type": "application/json",
        "X-Twitter-Active-User": "yes",
        "X-Twitter-Client-Language": "en",
        "X-Csrf-Token": ct0,
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        "Referer": "https://x.com/",
        "Origin": "https://x.com",
    }


def extract_tweets_and_cursor(response_json: dict) -> tuple[list[dict], str | None]:
    tweets = []
    cursor = None
    bottom_cursor = None
    try:
        instructions = (
            response_json.get("data", {})
            .get("user", {})
            .get("result", {})
            .get("timeline_v2", {})
            .get("timeline", {})
            .get("instructions", [])
        )
        for instr in instructions:
            entries = instr.get("entries", [])
            if not entries and instr.get("type") != "TimelineAddEntries":
                continue
            for entry in entries:
                content = entry.get("content", {})
                entry_type = content.get("entryType", "")

                if entry_type == "TimelineTimelineItem":
                    tweet_result = (
                        content.get("itemContent", {}).get("tweet_results", {}).get("result", {})
                    )
                    if tweet_result:
                        tweets.append(tweet_result)

                elif entry_type == "TimelineTimelineCursor":
                    if content.get("cursorType") == "Bottom":
                        bottom_cursor = content.get("value")

            pin_entries = instr.get("entries", [])
            for entry in pin_entries:
                content = entry.get("content", {})
                if content.get("entryType") == "TimelineTimelineCursor":
                    if content.get("cursorType") == "Bottom":
                        bottom_cursor = content.get("value")

        cursor = bottom_cursor
    except Exception as e:
        log.warning("Parse error in extract_tweets_and_cursor: %s", e)

    return tweets, cursor


async def fetch_user_tweets(
    session: aiohttp.ClientSession,
    user_id: str,
    cookies: dict,
    ct0: str,
    cursor: str | None = None,
    proxy: str | None = None,
) -> tuple[list[dict], str | None] | None:
    variables = {
        "userId": user_id,
        "count": TWEETS_PER_REQUEST,
        "includePromotedContent": False,
        "withQuickPromoteEligibilityTweetFields": True,
        "withVoice": True,
        "withV2Timeline": True,
    }
    if cursor:
        variables["cursor"] = cursor

    params = {
        "variables": json.dumps(variables),
        "features": json.dumps(GRAPHQL_FEATURES),
        "fieldToggles": json.dumps({"withArticleRichContentState": False}),
    }

    url = f"{GRAPHQL_BASE}/{USER_TWEETS_QUERY_ID}/UserTweets"
    headers = build_graphql_headers(cookies, ct0)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(
                url,
                params=params,
                headers=headers,
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status == 429:
                    wait = RETRY_DELAY * (2**attempt)
                    log.warning(
                        "Rate limited (429). Waiting %ds before retry %d/%d",
                        wait,
                        attempt,
                        MAX_RETRIES,
                    )
                    await asyncio.sleep(wait)
                    continue
                if resp.status != 200:
                    body = await resp.text()
                    log.error("GraphQL returned %d: %s", resp.status, body[:300])
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_DELAY)
                        continue
                    return None
                data = await resp.json()
                return extract_tweets_and_cursor(data)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Request error (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
                continue
            return None
    return None


async def paginate_all(
    target_user: str,
    max_tweets: int,
    proxy: str | None = None,
) -> list[dict]:
    log.info("Starting pagination for @%s (max %d tweets)", target_user, max_tweets)

    cookies = await get_cookies_from_session()
    ct0, auth_token = await get_ct0_and_auth(cookies)
    log.info("Got %d cookies, ct0=%s...", len(cookies), ct0[:8] if ct0 else "NONE")

    connector = aiohttp.TCPConnector(limit=ASYNC_SEMAPHORE)
    async with aiohttp.ClientSession(cookies=cookies, connector=connector) as session:
        user_id = await resolve_user_id(session, target_user, cookies, ct0)
        if not user_id:
            log.error("Cannot resolve user ID for @%s. Aborting.", target_user)
            return []

        all_tweets: list[dict] = []
        cursor = None
        page = 0
        consecutive_empty = 0

        while len(all_tweets) < max_tweets:
            page += 1
            log.info(
                "Page %d | cursor=%s | collected=%d",
                page,
                (cursor or "initial")[:12] if cursor else "initial",
                len(all_tweets),
            )

            result = await fetch_user_tweets(session, user_id, cookies, ct0, cursor, proxy)
            if result is None:
                log.error("Request failed on page %d. Stopping.", page)
                break

            tweets, new_cursor = result
            if not tweets:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    log.info("3 consecutive empty pages. Stopping.")
                    break
            else:
                consecutive_empty = 0

            all_tweets.extend(tweets)
            log.info("  Got %d tweets (total: %d)", len(tweets), len(all_tweets))

            if new_cursor:
                cursor = new_cursor
            else:
                log.info("No next cursor returned. Reached end of timeline.")
                break

            if len(all_tweets) % CHECKPOINT_INTERVAL == 0 and len(all_tweets) > 0:
                checkpoint_tweets = deduplicate(all_tweets)
                save_json(
                    checkpoint_tweets, f"{target_user}_checkpoint_{len(checkpoint_tweets)}.json"
                )
                log.info("Checkpoint saved at %d unique tweets", len(checkpoint_tweets))

            await asyncio.sleep(REQUEST_DELAY_SECONDS)

    all_tweets = deduplicate(all_tweets)
    log.info("Total unique tweets collected: %d", len(all_tweets))
    output_file = save_json(all_tweets, f"tweets_{target_user}_full.json")
    log.info("Final output: %s", output_file)
    return all_tweets


def main():
    parser = argparse.ArgumentParser(description="Cursor-paginated GraphQL tweet collector")
    parser.add_argument("--user", default=DEFAULT_TARGET, help="Target Twitter username")
    parser.add_argument(
        "--userid", default=None, help="Override numeric user ID (skips resolution)"
    )
    parser.add_argument(
        "--max", type=int, default=DEFAULT_MAX_TWEETS, help="Maximum tweets to collect"
    )
    parser.add_argument(
        "--proxy", default=None, help="HTTP proxy URL (e.g. http://user:pass@host:port)"
    )
    args = parser.parse_args()

    if args.userid:
        KNOWN_USER_IDS[args.user.lower()] = args.userid

    tweets = asyncio.run(paginate_all(args.user, args.max, args.proxy))

    if tweets:
        output_file = save_json(tweets, f"tweets_{args.user}_browser.json")
        log.info("Saved to %s", output_file)

        csv_path = Path(output_file).with_suffix(".csv")
        try:
            from export_csv import convert_json_to_csv

            stats = convert_json_to_csv(Path(output_file), csv_path, dedup=True)
            log.info("CSV: %d tweets → %s", stats["output_rows"], csv_path)
        except Exception as e:
            log.warning("CSV conversion failed (JSON still valid): %s", e)

        print(f"\nCollected {len(tweets)} unique tweets from @{args.user}")
        print(f"Preview (first 3):")
        for i, t in enumerate(tweets[:3]):
            cleaned = clean_tweet(t)
            print(f"  [{i + 1}] {cleaned['created_at']} | {cleaned['text'][:80]}...")
    else:
        print("No tweets collected. Check logs for errors.")


if __name__ == "__main__":
    main()
