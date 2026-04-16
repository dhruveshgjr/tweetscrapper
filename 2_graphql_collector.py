#!/usr/bin/env python3
"""
2_graphql_collector.py
Intercept GraphQL responses from a Twitter/X profile page using Playwright.
Collects tweets via scrolling + cursor pagination fallback.

Usage:
  python3 2_graphql_collector.py --user elonmusk --max 1000
  python3 2_graphql_collector.py --headless --max 1000 --scroll-rounds 150
"""

import argparse
import asyncio
import json
from pathlib import Path

from patchright.async_api import async_playwright

from config import (
    USER_DATA_DIR,
    DEFAULT_TARGET,
    GRAPHQL_FEATURES,
    KNOWN_USER_IDS,
    log,
    save_json,
    clean_tweet,
)
from export_csv import convert_json_to_csv

SCROLL_ROUNDS = 120
SCROLL_DELAY_BASE = 3.0
CHECKPOINT_INTERVAL = 250

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--window-size=1280,800",
]

TWEET_ENDPOINTS = [
    "UserTweets",
    "UserByScreenName",
    "UserMedia",
    "UserLikes",
    "TweetDetail",
    "UserResult",
]

CURSOR_QUERY_ID = "6fWQaBPK51aGyC_VC7t9GQ"


def get_author_screen_name(tweet: dict) -> str:
    core = tweet.get("core", {})
    user_results = core.get("user_results", {})
    result = user_results.get("result", {})

    if result.get("__typename") == "User":
        core_data = result.get("core", {})
        if core_data.get("screen_name"):
            return core_data.get("screen_name", "")
        if core_data.get("legacy", {}).get("screen_name"):
            return core_data.get("legacy", {}).get("screen_name", "")

    if result.get("__typename") == "User":
        legacy = result.get("legacy", {})
        if legacy.get("screen_name"):
            return legacy.get("screen_name", "")

    return ""


def extract_tweets_from_response(data):
    tweets = []
    if isinstance(data, dict):
        if "tweet_results" in data:
            result = data["tweet_results"].get("result")
            if result:
                tweets.append(result)

        if "legacy" in data and "full_text" in data.get("legacy", {}):
            if data not in tweets:
                tweets.append(data)

        for v in data.values():
            tweets.extend(extract_tweets_from_response(v))
    elif isinstance(data, list):
        for item in data:
            tweets.extend(extract_tweets_from_response(item))
    return tweets


def extract_cursor_from_response(data) -> str | None:
    try:
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
                    if content.get("entryType") == "TimelineTimelineCursor":
                        if content.get("cursorType") == "Bottom":
                            return content.get("value")
    except Exception:
        pass
    return None


def is_tweet_endpoint(url: str) -> bool:
    for ep in TWEET_ENDPOINTS:
        if ep in url:
            return True
    return False


async def collect_tweets(target_user: str, max_tweets: int, headless: bool, scroll_rounds: int):
    async with async_playwright() as p:
        # Use non-persistent browser - sessions from persistent context seem to have issues
        browser = await p.chromium.launch(
            headless=headless,
            args=STEALTH_ARGS,
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
        )
        page = await context.new_page()

        # Load cookies from persistent session
        try:
            await context.add_cookies(
                [
                    {
                        "name": "auth_token",
                        "value": "05d601f5970a86bdb4b96f82d675ecaa3bf44e86",
                        "domain": ".x.com",
                        "path": "/",
                    },
                    {
                        "name": "ct0",
                        "value": "133262cdd058ad80a0e1da2a011f2b0d84571d2b277836599506c37fd982c2355fc2fafe224b94557a8d06332a1118ab7d23c44e8f05ab04201e28cdfb4386f9c05ec7f9aac9abbbf6949d4443db1d56",
                        "domain": ".x.com",
                        "path": "/",
                    },
                ]
            )
        except Exception as e:
            log.warning(f"Could not load cookies: {e}")

        # More aggressive stealth
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        """)

        # Wait for session to fully load
        await page.goto("https://x.com", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)

        tweets_data = []
        seen_ids = set()
        next_cursor = None

        async def handle_response(response):
            nonlocal next_cursor
            url = response.url
            if "graphql" not in url:
                return
            if not is_tweet_endpoint(url):
                return
            try:
                data = await response.json()

                cursor = extract_cursor_from_response(data)
                if cursor:
                    if not next_cursor:
                        next_cursor = cursor
                        log.info("Captured cursor: %s...", cursor[:60])
                else:
                    instructions = (
                        data.get("data", {})
                        .get("user", {})
                        .get("result", {})
                        .get("timeline_v2", {})
                        .get("timeline", {})
                        .get("instructions", [])
                    )
                    has_tweets = any(i.get("type") == "TimelineAddEntries" for i in instructions)
                    if has_tweets and not next_cursor:
                        log.debug("Response has tweets but no cursor found")

                extracted = extract_tweets_from_response(data)

                for t in extracted:
                    tid = t.get("rest_id") or t.get("legacy", {}).get("id_str")
                    if not tid:
                        continue

                    author = get_author_screen_name(t)
                    if author.lower() != target_user.lower():
                        continue

                    if tid not in seen_ids:
                        seen_ids.add(tid)
                        tweets_data.append(t)
                        if len(tweets_data) % 20 == 0:
                            log.info("  Intercepted %d tweets...", len(tweets_data))

                        if len(tweets_data) % CHECKPOINT_INTERVAL == 0:
                            checkpoint_file = save_json(
                                tweets_data, f"tweets_{target_user}_checkpoint.json"
                            )
                            log.info("Checkpoint saved: %s", checkpoint_file)
            except Exception:
                pass

        page.on("response", handle_response)

        log.info("Using authenticated session [%s]", "headless" if headless else "visible")
        log.info(
            "Target: @%s | Max: %d | Scroll rounds: %d", target_user, max_tweets, scroll_rounds
        )

        await page.goto(
            f"https://x.com/{target_user}", wait_until="domcontentloaded", timeout=60000
        )
        await asyncio.sleep(8)

        user_id = KNOWN_USER_IDS.get(target_user.lower())
        log.info("User ID: %s", user_id)

        try:
            posts_tab = page.locator('a[href$="/posts"], a[href$="/posts/with_replies"]').first
            if await posts_tab.count() > 0:
                await posts_tab.click()
                log.info("Clicked Posts tab")
                await asyncio.sleep(3)
            else:
                tabs = page.locator('a[role="tab"]')
                if await tabs.count() > 0:
                    await tabs.first.click()
                    log.info("Clicked first tab")
                    await asyncio.sleep(3)
        except Exception as e:
            log.info("Tab click: %s", e)

        try:
            await page.wait_for_selector("article", timeout=15000)
            log.info("Found tweet articles")
        except Exception:
            await page.evaluate("window.scrollBy(0, 500)")
            await asyncio.sleep(2)

        no_new_tweets_count = 0
        for i in range(scroll_rounds):
            prev_count = len(tweets_data)

            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            jitter = SCROLL_DELAY_BASE + (i % 3) * 0.7
            await asyncio.sleep(jitter)

            new_count = len(tweets_data)

            if new_count == prev_count:
                no_new_tweets_count += 1
                if no_new_tweets_count == 3:
                    log.info("No new tweets - pressing End key")
                    await page.keyboard.press("End")
                    await asyncio.sleep(3)
                if no_new_tweets_count >= 10:
                    log.info("No new tweets after 10 scrolls – trying cursor fallback")
                    break
            else:
                no_new_tweets_count = 0

            log.info("Scroll %d/%d — %d tweets", i + 1, scroll_rounds, new_count)

            if new_count >= max_tweets:
                log.info("Reached max tweets target")
                break

        if len(tweets_data) < max_tweets and next_cursor:
            log.info(
                "Switching to cursor pagination (cursor: %s...)",
                next_cursor[:30] if next_cursor else "None",
            )

            cursor_fetch_count = 0
            while len(tweets_data) < max_tweets and next_cursor:
                cursor_fetch_count += 1
                log.info(
                    "Cursor fetch %d | cursor: %s... | collected: %d",
                    cursor_fetch_count,
                    next_cursor[:20],
                    len(tweets_data),
                )

                try:
                    result = await page.evaluate(f"""
                        async () => {{
                            const url = 'https://x.com/i/api/graphql/{CURSOR_QUERY_ID}/UserTweets';
                            const vars = {{
                                userId: '{user_id}',
                                count: 40,
                                cursor: '{next_cursor}',
                                includePromotedContent: false,
                                withQuickPromoteEligibilityTweetFields: true,
                                withVoice: true,
                                withV2Timeline: true
                            }};
                            const features = {json.dumps(GRAPHQL_FEATURES)};
                            const params = new URLSearchParams({{
                                variables: JSON.stringify(vars),
                                features: JSON.stringify(features)
                            }});
                            const res = await fetch(`${{url}}?${{params}}`, {{ credentials: 'include' }});
                            return await res.json();
                        }}
                    """)

                    if not result or not result.get("data"):
                        log.warning("Cursor fetch returned empty data")
                        break

                    new_tweets = extract_tweets_from_response(result)
                    new_cursor = extract_cursor_from_response(result)

                    new_count = 0
                    for t in new_tweets:
                        tid = t.get("rest_id") or t.get("legacy", {}).get("id_str")
                        if tid and tid not in seen_ids:
                            seen_ids.add(tid)
                            tweets_data.append(t)
                            new_count += 1

                    log.info(
                        "  Cursor got %d tweets (%d new) | total: %d",
                        len(new_tweets),
                        new_count,
                        len(tweets_data),
                    )

                    if not new_cursor:
                        log.info("No more cursor – timeline end")
                        break

                    next_cursor = new_cursor
                    await asyncio.sleep(1.5)

                    if len(tweets_data) % CHECKPOINT_INTERVAL == 0:
                        checkpoint_file = save_json(
                            tweets_data, f"tweets_{target_user}_checkpoint.json"
                        )
                        log.info("Checkpoint saved: %s", checkpoint_file)

                except Exception as e:
                    log.error("Cursor fetch failed: %s", e)
                    break

        log.info("Collected %d raw tweets", len(tweets_data))
        await browser.close()

    return tweets_data


def main():
    parser = argparse.ArgumentParser(description="Browser-based tweet collector")
    parser.add_argument("--user", default=DEFAULT_TARGET, help="Target username")
    parser.add_argument("--max", type=int, default=1000, help="Max tweets to collect")
    parser.add_argument("--headless", action="store_true", help="Run headless")
    parser.add_argument(
        "--scroll-rounds", type=int, default=SCROLL_ROUNDS, help="Number of scroll attempts"
    )
    args = parser.parse_args()

    tweets = asyncio.run(collect_tweets(args.user, args.max, args.headless, args.scroll_rounds))

    if not tweets:
        log.error("No tweets collected")
        return

    output_file = save_json(tweets, f"tweets_{args.user}_browser.json")
    log.info("Saved %d tweets to %s", len(tweets), output_file)

    csv_path = Path(output_file).with_suffix(".csv")
    report_path = csv_path.with_suffix(".report.json")
    try:
        stats = convert_json_to_csv(Path(output_file), csv_path, report_path, dedup=True)
        log.info("CSV: %d rows → %s", stats["output_rows"], csv_path)
        log.info("Report: %s", report_path)
    except Exception as e:
        log.warning("CSV conversion failed: %s", e)

    print(f"\nCollected {len(tweets)} tweets from @{args.user}")
    for idx, t in enumerate(tweets[:5]):
        text = t.get("legacy", {}).get("full_text", "")[:80]
        created = t.get("legacy", {}).get("created_at", "")
        print(f"  [{idx + 1}] {created} | {text}...")
    if len(tweets) > 5:
        print(f"  ... and {len(tweets) - 5} more")


if __name__ == "__main__":
    main()
