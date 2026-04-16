#!/usr/bin/env python3
"""
multi_tab_collector.py – Collect from Posts, Replies, Media, Likes tabs
"""

import asyncio
import json
from pathlib import Path

from patchright.async_api import async_playwright

from config import USER_DATA_DIR, GRAPHQL_FEATURES, KNOWN_USER_IDS, log, save_json
from export_csv import convert_json_to_csv
import sys
import importlib.util
from pathlib import Path

# Import 2_graphql_collector.py functions
spec = importlib.util.spec_from_file_location("collector", "2_graphql_collector.py")
collector = importlib.util.module_from_spec(spec)
sys.modules["collector"] = collector
spec.loader.exec_module(collector)

extract_tweets_from_response = collector.extract_tweets_from_response
is_tweet_endpoint = collector.is_tweet_endpoint

TARGET_USER = "elonmusk"
MAX_PER_TAB = 500
SCROLL_ROUNDS = 40

TABS = [
    {"name": "posts", "url_suffix": ""},
    {"name": "replies", "url_suffix": "/with_replies"},
    {"name": "media", "url_suffix": "/media"},
    {"name": "likes", "url_suffix": "/likes"},
]


async def collect_from_tab(tab_name, url_suffix):
    log.info(f"Collecting from tab: {tab_name}")

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

        url = f"https://x.com/{TARGET_USER}{url_suffix}"
        log.info(f"Loading: {url}")

        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        try:
            await page.wait_for_selector("article", timeout=15000)
        except:
            log.warning(f"Tab {tab_name}: No articles found")

        tweets_data = []
        seen_ids = set()

        async def handle_response(response):
            if "graphql" not in response.url:
                return
            if not is_tweet_endpoint(response.url):
                return

            try:
                data = await response.json()
                extracted = extract_tweets_from_response(data)

                for t in extracted:
                    tid = t.get("rest_id") or t.get("legacy", {}).get("id_str")
                    if tid and tid not in seen_ids:
                        seen_ids.add(tid)
                        tweets_data.append(t)

                        if len(tweets_data) % 50 == 0:
                            log.info(f"  {tab_name}: {len(tweets_data)} tweets...")
            except:
                pass

        page.on("response", handle_response)

        # Scroll to collect tweets
        no_new_count = 0
        for i in range(SCROLL_ROUNDS):
            prev = len(tweets_data)
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await asyncio.sleep(2.5)

            if len(tweets_data) >= MAX_PER_TAB:
                log.info(f"Tab {tab_name}: Reached max {MAX_PER_TAB}")
                break

            if len(tweets_data) == prev:
                no_new_count += 1
                if no_new_count >= 5:
                    log.info(f"Tab {tab_name}: No new tweets after 5 scrolls")
                    break
            else:
                no_new_count = 0

        await context.close()
        log.info(f"Tab {tab_name}: collected {len(tweets_data)} tweets")
        return tweets_data


async def main():
    log.info("Multi-Tab Collector: Posts, Replies, Media, Likes")

    all_tweets = []
    seen_global = set()

    for tab in TABS:
        try:
            tweets = await collect_from_tab(tab["name"], tab["url_suffix"])

            new_count = 0
            for t in tweets:
                tid = t.get("rest_id") or t.get("legacy", {}).get("id_str")
                if tid and tid not in seen_global:
                    seen_global.add(tid)
                    all_tweets.append(t)
                    new_count += 1

            log.info(f"Tab {tab['name']}: {len(tweets)} collected, {new_count} new")

        except Exception as e:
            log.error(f"Tab {tab['name']} failed: {e}")

    log.info(f"Final total: {len(all_tweets)} unique tweets")

    if all_tweets:
        out_file = save_json(all_tweets, f"tweets_{TARGET_USER}_multitab.json")
        csv_path = Path(out_file).with_suffix(".csv")
        report_path = Path(out_file).with_suffix(".report.json")
        stats = convert_json_to_csv(Path(out_file), csv_path, report_path, dedup=True)
        log.info(f"CSV: {stats['output_rows']} rows -> {csv_path}")
    else:
        log.error("No tweets collected")


if __name__ == "__main__":
    asyncio.run(main())
