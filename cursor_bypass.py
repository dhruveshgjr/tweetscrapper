#!/usr/bin/env python3
"""
cursor_bypass.py – Cursor manipulation to fetch historical tweets beyond X.com UI limit
"""

import asyncio
import aiohttp
import base64
import json
import struct
import time
from pathlib import Path

from patchright.async_api import async_playwright

from config import USER_DATA_DIR, GRAPHQL_FEATURES, KNOWN_USER_IDS, log, save_json
from export_csv import convert_json_to_csv

TARGET_USER = "elonmusk"
MAX_TWEETS = 3000
TWEETS_PER_PAGE = 40
REQUEST_DELAY = 1.0
QUERY_ID = "6fWQaBPK51aGyC_VC7t9GQ"


def decode_cursor(cursor: str) -> dict:
    try:
        decoded = base64.b64decode(cursor)
        if len(decoded) > 12:
            timestamp = struct.unpack("<I", decoded[8:12])[0]
            return {"timestamp": timestamp, "raw": decoded}
    except Exception:
        pass
    return {"timestamp": None, "raw": None}


def encode_cursor(timestamp: int, original_raw: bytes) -> str:
    if not original_raw:
        return None
    new_raw = bytearray(original_raw)
    struct.pack_into("<I", new_raw, 8, timestamp)
    return base64.b64encode(new_raw).decode("ascii")


async def get_fresh_cookies_and_user_id():
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=True,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = await context.new_page()

        await page.goto(f"https://x.com/{TARGET_USER}", wait_until="domcontentloaded")

        try:
            await page.wait_for_selector("article", timeout=15000)
        except:
            log.warning("No articles found, trying alternative")

        cookies = await context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}

        user_id = KNOWN_USER_IDS.get(TARGET_USER.lower())

        cursor = None

        def capture(response):
            nonlocal cursor
            if "UserTweets" in response.url and "graphql" in response.url:
                try:
                    data = (
                        response._impl_obj._response._response._body_as_json
                        if hasattr(response, "_impl_obj")
                        else None
                    )
                except:
                    pass

        await page.evaluate("window.scrollBy(0, 2000)")
        await asyncio.sleep(3)

        await context.close()
        return cookie_dict, user_id, cursor


async def fetch_tweets_page(session, user_id, cursor):
    url = f"https://x.com/i/api/graphql/{QUERY_ID}/UserTweets"
    variables = {
        "userId": user_id,
        "count": TWEETS_PER_PAGE,
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
    }
    headers = {
        "authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
        "content-type": "application/json",
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2C",
    }

    try:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status != 200:
                log.error("HTTP %d", resp.status)
                return [], None

            data = await resp.json()
            instructions = (
                data.get("data", {})
                .get("user", {})
                .get("result", {})
                .get("timeline_v2", {})
                .get("timeline", {})
                .get("instructions", [])
            )

            tweets = []
            next_cursor = None

            for instr in instructions:
                if instr.get("type") == "TimelineAddEntries":
                    for entry in instr.get("entries", []):
                        content = entry.get("content", {})
                        if content.get("entryType") == "TimelineTimelineItem":
                            tweet_result = (
                                content.get("itemContent", {})
                                .get("tweet_results", {})
                                .get("result", {})
                            )
                            if tweet_result:
                                tweets.append(tweet_result)
                        elif content.get("entryType") == "TimelineTimelineCursor":
                            if content.get("cursorType") == "Bottom":
                                next_cursor = content.get("value")

            return tweets, next_cursor
    except Exception as e:
        log.error(f"Fetch error: {e}")
        return [], None


async def fetch_with_cursor_from_browser():
    """Get a valid cursor by intercepting from browser"""
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )

        if context.pages:
            page = context.pages[0]
        else:
            page = await context.new_page()

        await page.goto(f"https://x.com/{TARGET_USER}", wait_until="domcontentloaded")

        try:
            await page.wait_for_selector("article", timeout=15000)
        except:
            pass

        captured_cursor = [None]

        async def capture_response(response):
            url = response.url
            if "graphql" in url and "UserTweets" in url:
                try:
                    data = await response.json()
                    instructions = (
                        data.get("data", {})
                        .get("user", {})
                        .get("result", {})
                        .get("timeline_v2", {})
                        .get("timeline", {})
                        .get("instructions", [])
                    )
                    for instr in instructions:
                        if instr.get("type") == "TimelineAddEntries":
                            for entry in instr.get("entries", []):
                                content = entry.get("content", {})
                                if (
                                    content.get("entryType") == "TimelineTimelineCursor"
                                    and content.get("cursorType") == "Bottom"
                                ):
                                    captured_cursor[0] = content.get("value")
                                    log.info("Captured cursor: %s...", captured_cursor[0][:40])
                                    return
                except:
                    pass

        page.on("response", capture_response)
        await page.evaluate("window.scrollBy(0, 3000)")
        await asyncio.sleep(5)

        await context.close()

        return captured_cursor[0]


async def main():
    log.info("Cursor Bypass Mode: Collecting historical tweets")

    cursor = await fetch_with_cursor_from_browser()

    if not cursor:
        log.error("Could not capture cursor from browser")
        log.info("Falling back to normal pagination...")
        cursor = None
    else:
        decoded = decode_cursor(cursor)
        if decoded["timestamp"]:
            log.info(f"Base timestamp: {time.ctime(decoded['timestamp'])}")
            # Try jumping back 30 days
            new_ts = decoded["timestamp"] - (30 * 86400)
            cursor = encode_cursor(new_ts, decoded["raw"])
            log.info(f"Modified cursor: jumping to {time.ctime(new_ts)}")
        else:
            log.warning("Could not decode cursor, using as-is")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=True,
        )
        cookies = await context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}
        user_id = KNOWN_USER_IDS.get(TARGET_USER.lower())
        await context.close()

    all_tweets = []
    seen_ids = set()

    async with aiohttp.ClientSession(cookies=cookie_dict) as session:
        page = 0
        while len(all_tweets) < MAX_TWEETS:
            page += 1
            log.info(f"Page {page} | collected: {len(all_tweets)}/{MAX_TWEETS}")

            tweets, cursor = await fetch_tweets_page(session, user_id, cursor)

            if not tweets:
                log.info("No more tweets - stopping")
                break

            new_count = 0
            for t in tweets:
                tid = t.get("rest_id") or t.get("legacy", {}).get("id_str")
                if tid and tid not in seen_ids:
                    seen_ids.add(tid)
                    all_tweets.append(t)
                    new_count += 1

            log.info(f"  Got {len(tweets)} tweets ({new_count} new), total: {len(all_tweets)}")

            if not cursor:
                log.info("No more cursor available")
                break

            await asyncio.sleep(REQUEST_DELAY)

    log.info(f"Total collected: {len(all_tweets)} tweets")

    if all_tweets:
        out_file = save_json(all_tweets, f"tweets_{TARGET_USER}_bypass.json")
        csv_path = Path(out_file).with_suffix(".csv")
        stats = convert_json_to_csv(Path(out_file), csv_path, dedup=True)
        log.info(f"CSV: {stats['output_rows']} rows -> {csv_path}")
    else:
        log.error("No tweets collected")


if __name__ == "__main__":
    asyncio.run(main())
