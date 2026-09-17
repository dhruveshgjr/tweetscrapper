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
import re
import aiohttp
from pathlib import Path
from urllib.parse import urlparse, parse_qs
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

FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


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


LIVE_OPS = ("UserTweets", "UserOriginalsTimeline", "UserTweetsAndReplies")


async def discover_live_ids(username: str):
    """One short browser pass to capture live queryIds + rest_id.
    X rotates queryIds weekly; hardcoded IDs 404.
    Returns (user_id, timeline_query_id, timeline_op, bs_query_id, live_params).
    live_params carries the verbatim live features/fieldToggles/variables
    template — replayed exactly by fetch_user_tweets (stale flags 404)."""
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR, headless=True,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        found = {"user_id": None, "tl_id": None, "tl_op": None, "bs_id": None,
                 "tl_features": None, "tl_toggles": None, "tl_vars": None,
                 "user_agent": None}

        async def on_resp(resp):
            url = resp.url
            if "/graphql/" not in url:
                return
            m = re.search(r"/graphql/([^/]+)/(\w+)", url)
            if not m:
                return
            qid, op = m.group(1), m.group(2)
            if op == "UserByScreenName":
                found["bs_id"] = qid
                try:
                    data = await resp.json()
                    uid = (data.get("data", {}).get("user", {})
                           .get("result", {}).get("rest_id"))
                    if uid:
                        found["user_id"] = uid
                except Exception:
                    pass
            elif op in LIVE_OPS and not found["tl_id"]:
                found["tl_id"] = qid
                found["tl_op"] = op
                try:
                    req_headers = resp.request.headers
                    ua = req_headers.get("user-agent")
                    if ua:
                        found["user_agent"] = ua
                    q = parse_qs(urlparse(resp.request.url).query)
                    if q.get("features"):
                        found["tl_features"] = q["features"][0]
                    if q.get("fieldToggles"):
                        found["tl_toggles"] = q["fieldToggles"][0]
                    if q.get("variables"):
                        found["tl_vars"] = q["variables"][0]
                except Exception:
                    pass
                if not found["user_id"]:
                    try:
                        data = await resp.json()
                        uid = (data.get("data", {}).get("user", {})
                               .get("result", {}).get("rest_id"))
                        if uid:
                            found["user_id"] = uid
                    except Exception:
                        pass

        page.on("response", on_resp)
        try:
            await page.goto(f"https://x.com/{username}",
                            wait_until="domcontentloaded", timeout=60000)
        except Exception as ex:
            log.warning("Discovery nav error: %s", ex)
        for _ in range(15):
            if found["user_id"] and found["tl_id"]:
                break
            await asyncio.sleep(1)
        await context.close()
    log.info("Discovered: user_id=%s tl_op=%s tl_queryId=%s bs_queryId=%s",
             found["user_id"], found["tl_op"], found["tl_id"], found["bs_id"])
    live_params = {"features": found["tl_features"], "toggles": found["tl_toggles"],
                   "vars": found["tl_vars"], "user_agent": found["user_agent"]}
    return found["user_id"], found["tl_id"], found["tl_op"], found["bs_id"], live_params


async def resolve_user_id(
    session: aiohttp.ClientSession, username: str, cookies: dict, ct0: str,
    bs_id: str | None = None,
    user_agent: str | None = None,
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
    url = f"{GRAPHQL_BASE}/{bs_id or USER_BY_SCREEN_NAME_QUERY_ID}/UserByScreenName"
    headers = build_graphql_headers(cookies, ct0, user_agent)

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


def build_graphql_headers(cookies: dict, ct0: str, user_agent: str | None = None) -> dict:
    return {
        "Authorization": f"Bearer {BEARER_TOKEN}",
        "Content-Type": "application/json",
        "X-Twitter-Active-User": "yes",
        "X-Twitter-Client-Language": "en",
        "X-Csrf-Token": ct0,
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        "Referer": "https://x.com/",
        "Origin": "https://x.com",
        "Accept": "*/*",
        # X's edge 404s non-browser User-Agents (aiohttp default included).
        # Prefer the live UA captured during discovery; fallback to current Chrome.
        "User-Agent": user_agent or FALLBACK_USER_AGENT,
    }


def extract_tweets_and_cursor(response_json: dict) -> tuple[list[dict], str | None]:
    tweets = []
    cursor = None
    bottom_cursor = None
    try:
        result = response_json.get("data", {}).get("user", {}).get("result", {})
        # X renamed the container: older ops use timeline_v2, current
        # UserOriginalsTimeline returns timeline. Accept both.
        container = result.get("timeline_v2", result.get("timeline", {}))
        instructions = container.get("timeline", {}).get("instructions", [])
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
    query_id: str = USER_TWEETS_QUERY_ID,
    op: str = "UserTweets",
    live_params: dict | None = None,
) -> tuple[list[dict], str | None] | None:
    live_params = live_params or {}
    params = None
    if live_params.get("vars") and live_params.get("features"):
        try:
            variables = json.loads(live_params["vars"])
            variables["userId"] = user_id
            variables["count"] = TWEETS_PER_REQUEST
            if cursor:
                variables["cursor"] = cursor
            else:
                variables.pop("cursor", None)
            params = {
                "variables": json.dumps(variables),
                "features": live_params["features"],
            }
            if live_params.get("toggles"):
                params["fieldToggles"] = live_params["toggles"]
        except Exception:
            params = None
    if params is None:
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

    url = f"{GRAPHQL_BASE}/{query_id}/{op}"
    headers = build_graphql_headers(
        cookies, ct0, (live_params or {}).get("user_agent"))

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
        user_id, live_tl_id, live_tl_op, live_bs_id, live_params = await discover_live_ids(target_user)
        if not user_id:
            user_id = await resolve_user_id(
                session, target_user, cookies, ct0, live_bs_id,
                live_params.get("user_agent"))
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

            result = await fetch_user_tweets(
                session, user_id, cookies, ct0, cursor, proxy,
                query_id=live_tl_id or USER_TWEETS_QUERY_ID,
                op=live_tl_op or "UserTweets",
                live_params=live_params,
            )
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
