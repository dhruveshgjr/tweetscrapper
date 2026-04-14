"""
2_graphql_collector.py
Intercept GraphQL responses from a Twitter/X profile page.
Week 1: No login (public). Week 2: Authenticated session.

Usage:
  python3 2_graphql_collector.py                  # authenticated, visible browser
  python3 2_graphql_collector.py --headless       # authenticated, headless
  python3 2_graphql_collector.py --no-login       # public (no session)
  python3 2_graphql_collector.py --user elonmusk  # custom target
  python3 2_graphql_collector.py --max 200        # collect up to 200 tweets
  python3 2_graphql_collector.py --debug          # pause before close, show URLs
"""

import argparse
import asyncio
import json
from pathlib import Path

from patchright.async_api import async_playwright

from config import (
    USER_DATA_DIR,
    DEFAULT_TARGET,
    log,
    save_json,
    clean_tweet,
    deduplicate,
)

from export_csv import convert_json_to_csv

SCROLL_ROUNDS = 15
SCROLL_DELAY_BASE = 2.5

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
    """
    Extract the screen_name of the tweet's author.
    Checks multiple possible paths in the nested JSON structure.
    """
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
    """
    Recursively extract all tweet objects from a GraphQL response.
    This collects raw tweet objects - filtering by author happens immediately after.
    Handles three cases:
    1. Tweet wrapped in tweet_results.result
    2. Direct tweet object with legacy.full_text
    3. Nested tweets within retweeted_status_result or quoted_status_result
    """
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


async def intercept_graphql(
    target_user: str, use_session: bool, max_tweets: int, headless: bool, debug: bool
):
    scroll_rounds = SCROLL_ROUNDS
    if not use_session:
        scroll_rounds = min(scroll_rounds, 8)

    async with async_playwright() as p:
        if use_session:
            context = await p.chromium.launch_persistent_context(
                USER_DATA_DIR,
                headless=headless,
                viewport={"width": 1280, "height": 800},
                args=STEALTH_ARGS,
                ignore_default_args=["--enable-automation"],
            )
            page = context.pages[0] if context.pages else await context.new_page()
        else:
            browser = await p.chromium.launch(
                headless=False,
                args=STEALTH_ARGS,
            )
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await context.new_page()

        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        tweets_data = []
        seen_ids = set()
        tweet_graphql_urls = []

        async def handle_response(response):
            url = response.url
            if "graphql" not in url:
                return
            if not is_tweet_endpoint(url):
                return
            tweet_graphql_urls.append(url)
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
                        if len(tweets_data) % 10 == 0:
                            log.info("  Intercepted %d tweets so far...", len(tweets_data))
            except Exception:
                pass

        page.on("response", handle_response)

        mode = "authenticated" if use_session else "public (no login)"
        vis = "headless" if headless else "visible"
        log.info("Using %s session [%s]", mode, vis)

        target_url = f"https://x.com/{target_user}"
        log.info("Navigating to %s", target_url)

        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log.warning("Navigation issue (continuing): %s", e)

        await asyncio.sleep(4)

        current_url = page.url
        log.info("Page URL after load: %s", current_url)

        try:
            posts_tab = page.locator('a[href$="/posts"], a[href$="/posts/with_replies"]').first
            if await posts_tab.count() > 0:
                await posts_tab.click()
                log.info("Clicked 'Posts' tab")
                await asyncio.sleep(3)
            else:
                tabs = page.locator('a[role="tab"]')
                tab_count = await tabs.count()
                log.info("Found %d tab links on page", tab_count)
                if tab_count > 0:
                    await tabs.first.click()
                    log.info("Clicked first tab")
                    await asyncio.sleep(3)
        except Exception as e:
            log.info("Tab click attempt: %s (will scroll anyway)", e)

        try:
            await page.wait_for_selector("article", timeout=15000)
            log.info("Found tweet articles on page")
        except Exception:
            log.warning("No <article> elements found yet")
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
                if no_new_tweets_count >= 3:
                    log.info("No new tweets for 3 scrolls — trying End key")
                    await page.keyboard.press("End")
                    await asyncio.sleep(3)
                    no_new_tweets_count = 0
            else:
                no_new_tweets_count = 0

            log.info(
                "Scroll %d/%d — %d tweets intercepted",
                i + 1,
                scroll_rounds,
                new_count,
            )

            if new_count >= max_tweets:
                break

        log.info(
            "Captured %d tweet-related GraphQL responses, %d raw tweets",
            len(tweet_graphql_urls),
            len(tweets_data),
        )

        if debug:
            log.info("Tweet GraphQL endpoint URLs intercepted:")
            for url in tweet_graphql_urls[:20]:
                endpoint = (
                    url.split("/graphql/")[1].split("/")[0] if "/graphql/" in url else url[:80]
                )
                log.info("  %s", endpoint)
            log.info("Total intercepted URLs: %d", len(tweet_graphql_urls))
            input("Press Enter to close browser...")

        log.info("Collected %d raw tweets", len(tweets_data))

        if use_session:
            await context.close()
        else:
            await browser.close()

    if not tweets_data:
        log.error("No tweets collected. Possible causes:")
        log.error("  1. Session expired — re-run: python 1_authenticator.py")
        log.error("  2. X.com showing Highlights tab — script now tries to click Posts tab")
        log.error("  3. Try with --debug to see which GraphQL endpoints are hit")
        print("\nNo tweets collected. Try:")
        print("  python3 2_graphql_collector.py --debug         # see what endpoints are called")
        print("  python3 2_graphql_collector.py --no-login      # without session")
        return []

    output_file = save_json(tweets_data, f"tweets_{target_user}_browser.json")
    log.info("Saved to %s", output_file)

    csv_path = Path(output_file).with_suffix(".csv")
    report_path = csv_path.with_suffix(".report.json")
    try:
        stats = convert_json_to_csv(Path(output_file), csv_path, report_path, dedup=True)
        log.info("CSV: %d tweets → %s", stats["output_rows"], csv_path)
        log.info("Report: %s", report_path)
    except Exception as e:
        log.warning("CSV conversion failed (JSON still valid): %s", e)

    cleaned = [clean_tweet(t) for t in tweets_data[:max_tweets]]
    print(f"\nCollected {len(cleaned)} tweets from @{target_user}")
    for idx, t in enumerate(cleaned[:5]):
        print(f"  [{idx + 1}] {t['created_at']} | {t['text'][:80]}...")
    if len(cleaned) > 5:
        print(f"  ... and {len(cleaned) - 5} more (see {output_file})")

    return tweets_data


def main():
    parser = argparse.ArgumentParser(description="Browser-based GraphQL tweet collector")
    parser.add_argument("--user", default=DEFAULT_TARGET, help="Target username")
    parser.add_argument("--no-login", action="store_true", help="Run without saved session")
    parser.add_argument("--max", type=int, default=100, help="Max tweets to collect")
    parser.add_argument(
        "--headless", action="store_true", help="Run in headless mode (default: visible)"
    )
    parser.add_argument(
        "--debug", action="store_true", help="Show GraphQL URLs and pause before close"
    )
    args = parser.parse_args()

    asyncio.run(
        intercept_graphql(args.user, not args.no_login, args.max, args.headless, args.debug)
    )


if __name__ == "__main__":
    main()
