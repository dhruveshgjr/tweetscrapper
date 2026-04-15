#!/usr/bin/env python3
"""
2_graphql_collector.py
Intercept GraphQL responses from a Twitter/X profile page using Playwright.
Collects tweets via scrolling with deduplication and checkpointing.

Usage:
  python3 2_graphql_collector.py --user elonmusk --max 500
  python3 2_graphql_collector.py --headless --max 500
"""

import argparse
import asyncio
from pathlib import Path

from patchright.async_api import async_playwright

from config import (
    USER_DATA_DIR,
    DEFAULT_TARGET,
    log,
    save_json,
    clean_tweet,
)
from export_csv import convert_json_to_csv

SCROLL_ROUNDS = 50
SCROLL_DELAY_BASE = 2.5
CHECKPOINT_INTERVAL = 200

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


def is_tweet_endpoint(url: str) -> bool:
    for ep in TWEET_ENDPOINTS:
        if ep in url:
            return True
    return False


async def collect_tweets(target_user: str, max_tweets: int, headless: bool):
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=headless,
            viewport={"width": 1280, "height": 800},
            args=STEALTH_ARGS,
            ignore_default_args=["--enable-automation"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        tweets_data = []
        seen_ids = set()

        async def handle_response(response):
            url = response.url
            if "graphql" not in url:
                return
            if not is_tweet_endpoint(url):
                return
            try:
                data = await response.json()
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
            "Target: @%s | Max: %d | Scroll rounds: %d", target_user, max_tweets, SCROLL_ROUNDS
        )

        await page.goto(
            f"https://x.com/{target_user}", wait_until="domcontentloaded", timeout=30000
        )
        await asyncio.sleep(4)

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
        for i in range(SCROLL_ROUNDS):
            prev_count = len(tweets_data)

            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            jitter = SCROLL_DELAY_BASE + (i % 3) * 0.7
            await asyncio.sleep(jitter)

            new_count = len(tweets_data)

            if new_count == prev_count:
                no_new_tweets_count += 1
                if no_new_tweets_count >= 3:
                    log.info("No new tweets - pressing End key")
                    await page.keyboard.press("End")
                    await asyncio.sleep(3)
                    no_new_tweets_count = 0
            else:
                no_new_tweets_count = 0

            log.info("Scroll %d/%d — %d tweets", i + 1, SCROLL_ROUNDS, new_count)

            if new_count >= max_tweets:
                log.info("Reached max tweets target")
                break

        log.info("Collected %d raw tweets", len(tweets_data))
        await context.close()

    return tweets_data


def main():
    parser = argparse.ArgumentParser(description="Browser-based tweet collector")
    parser.add_argument("--user", default=DEFAULT_TARGET, help="Target username")
    parser.add_argument("--max", type=int, default=500, help="Max tweets to collect")
    parser.add_argument("--headless", action="store_true", help="Run headless")
    args = parser.parse_args()

    tweets = asyncio.run(collect_tweets(args.user, args.max, args.headless))

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
