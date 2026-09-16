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
import re
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

SCROLL_ROUNDS = 250  # Aggressive scrolling
SCROLL_DELAY_BASE = 3.0
CHECKPOINT_INTERVAL = 100  # Validate every 100 tweets

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
    # X renamed profile timelines in 2025-2026 — these are the live ops
    # observed for @CREAWKenya (graphql_total=30, matched=1 bug).
    "UserOriginalsTimeline",
    "UserTweetsAndReplies",
    "ProfileSpotlightsQuery",
]

TIMELINE_OPS = (
    "UserTweets",
    "UserOriginalsTimeline",
    "UserTweetsAndReplies",
    "UserMedia",
    "UserLikes",
)

CURSOR_QUERY_ID = "6fWQaBPK51aGyC_VC7t9GQ"


def unwrap_tweet(node: dict) -> dict:
    """Unwrap TweetWithVisibilityResults / tweetWithVisibilityResults wrappers."""
    if not isinstance(node, dict):
        return node
    if node.get("__typename") == "TweetWithVisibilityResults" and isinstance(
        node.get("tweet"), dict
    ):
        return node["tweet"]
    # some payloads nest under 'tweet' with legacy inside
    t = node.get("tweet")
    if isinstance(t, dict) and "legacy" in t and "rest_id" in node:
        return node
    return node


def get_author_screen_name(tweet: dict) -> str:
    tweet = unwrap_tweet(tweet)
    # Shape 1: core.user_results.result.{legacy.screen_name | core.screen_name}
    try:
        result = tweet.get("core", {}).get("user_results", {}).get("result", {})
        if isinstance(result, dict):
            legacy_sn = result.get("legacy", {}).get("screen_name")
            if legacy_sn:
                return legacy_sn
            core_sn = result.get("core", {}).get("screen_name")
            if core_sn:
                return core_sn
            # legacy nested oddly: result.core.legacy?
            legacy2 = result.get("core", {}).get("legacy", {}).get("screen_name")
            if legacy2:
                return legacy2
    except Exception:
        pass
    # Shape 2: legacy.screen_name (rare) / author
    try:
        if tweet.get("legacy", {}).get("screen_name"):
            return tweet["legacy"]["screen_name"]
        if tweet.get("author", {}).get("screen_name"):
            return tweet["author"]["screen_name"]
    except Exception:
        pass
    return ""


def extract_tweets_from_response(data):
    """Handle TimelineAddEntries itemContent AND any legacy.full_text shape.

    Explicit path covers data.user.result.timeline_v2.timeline.instructions
    (UserTweets, UserOriginalsTimeline, UserTweetsAndReplies). Generic
    recursion is the safety net for future renames — any dict with
    legacy.full_text + rest_id/id_str is a tweet.
    """
    tweets = []
    if isinstance(data, dict):
        # Explicit timeline path (most reliable, avoids wrapper confusion)
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
                for entry in instr.get("entries", []):
                    content = entry.get("content", {})
                    if content.get("entryType") == "TimelineTimelineItem":
                        r = (
                            content.get("itemContent", {})
                            .get("tweet_results", {})
                            .get("result", {})
                        )
                        if r:
                            tweets.append(unwrap_tweet(r))
                    # threaded / conversation items
                    items = content.get("items", [])
                    for it in items:
                        r = (
                            it.get("item", {})
                            .get("itemContent", {})
                            .get("tweet_results", {})
                            .get("result", {})
                        )
                        if r:
                            tweets.append(unwrap_tweet(r))
        except Exception:
            pass

        if "tweet_results" in data:
            result = data["tweet_results"].get("result")
            if result:
                tweets.append(unwrap_tweet(result))

        if "legacy" in data and "full_text" in data.get("legacy", {}):
            if data not in tweets:
                tweets.append(unwrap_tweet(data))

        for v in data.values():
            # Skip already-handled timeline branch to avoid double-count
            # (dedup by rest_id happens later anyway, so just recurse).
            tweets.extend(extract_tweets_from_response(v))
    elif isinstance(data, list):
        for item in data:
            tweets.extend(extract_tweets_from_response(item))
    return tweets


def extract_cursor_from_response(data) -> str | None:
    # Fast path: known timeline_v2 shape.
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
    # Generic fallback: any TimelineTimelineCursor/Bottom anywhere in payload.
    # Handles renames (UserOriginalsTimeline) and nesting changes.
    try:
        return _find_bottom_cursor(data)
    except Exception:
        return None


def _find_bottom_cursor(node) -> str | None:
    if isinstance(node, dict):
        if (
            node.get("entryType") == "TimelineTimelineCursor"
            and node.get("cursorType") == "Bottom"
            and node.get("value")
        ):
            return node["value"]
        for v in node.values():
            found = _find_bottom_cursor(v)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_bottom_cursor(item)
            if found:
                return found
    return None


def is_tweet_endpoint(url: str) -> bool:
    for ep in TWEET_ENDPOINTS:
        if ep in url:
            return True
    return False


async def collect_tweets(target_user: str, max_tweets: int, headless: bool, scroll_rounds: int):
    async with async_playwright() as p:
        # Reuse authenticated session from 1_authenticator.py (./x_session)
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=headless,
            viewport={"width": 1280, "height": 800},
            args=STEALTH_ARGS,
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # Verify session cookies from authenticator
        cookies = await context.cookies()
        ct0_found = any(c["name"] == "ct0" for c in cookies)
        auth_found = any(c["name"] == "auth_token" for c in cookies)
        if not (ct0_found and auth_found):
            log.warning(
                "Session cookies incomplete in %s (ct0=%s, auth_token=%s). Re-run: python 1_authenticator.py",
                USER_DATA_DIR,
                ct0_found,
                auth_found,
            )

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
        # Diagnostics + dynamic discovery (X rotates query IDs weekly)
        stats = {
            "graphql_total": 0,
            "matched": 0,
            "extracted_raw": 0,
            "kept": 0,
            "filtered_out": 0,
        }
        seen_urls: set[str] = set()
        captured_query_id: str | None = None
        captured_timeline_op: str | None = None
        captured_user_id: str | None = KNOWN_USER_IDS.get(target_user.lower())
        debug_saved = False
        author_hist: dict[str, int] = {}

        async def handle_response(response):
            nonlocal next_cursor, captured_query_id, captured_timeline_op
            nonlocal captured_user_id, debug_saved
            url = response.url
            if "graphql" not in url:
                return
            stats["graphql_total"] += 1
            if url not in seen_urls:
                seen_urls.add(url)
                # log only operation names to avoid spam
                op = url.split("/graphql/")[-1][:80] if "/graphql/" in url else url[:80]
                log.info("GraphQL: %s", op)
            if not is_tweet_endpoint(url):
                return
            stats["matched"] += 1
            # Capture live Timeline queryId (replaces stale CURSOR_QUERY_ID).
            # X rotates these weekly; 2026 op is UserOriginalsTimeline, not UserTweets.
            for op_name in TIMELINE_OPS:
                if op_name in url and not captured_query_id:
                    m = re.search(r"/graphql/([^/]+)/" + re.escape(op_name), url)
                    if m:
                        captured_query_id = m.group(1)
                        captured_timeline_op = op_name
                        log.info(
                            "Captured live %s queryId: %s", op_name, captured_query_id
                        )
                    break
            try:
                data = await response.json()
            except Exception:
                return
            try:
                # Capture userId dynamically for unknown users (e.g. CREAWKenya)
                if not captured_user_id and "UserByScreenName" in url:
                    try:
                        uid = (
                            data.get("data", {})
                            .get("user", {})
                            .get("result", {})
                            .get("rest_id")
                        )
                        if uid:
                            captured_user_id = uid
                            log.info("Resolved @%s → %s (via UserByScreenName)", target_user, uid)
                    except Exception:
                        pass

                if not debug_saved and "UserByScreenName" not in url:
                    debug_saved = True
                    try:
                        save_json([{"url": url, "sample": data}], f"debug_{target_user}_sample.json")
                        log.info("Saved debug sample → output/debug_%s_sample.json", target_user)
                    except Exception:
                        pass

                cursor = extract_cursor_from_response(data)
                if cursor:
                    next_cursor = cursor  # always keep freshest Bottom cursor
                    if stats["matched"] <= 3:
                        log.info("Captured cursor: %s...", cursor[:60])

                extracted = extract_tweets_from_response(data)
                # extract_* uses explicit + generic paths so the same tweet can
                # appear 2-3x per payload — dedup here so stats are honest.
                # (final dedup is still via seen_ids below).
                _uniq = {}
                for _t in extracted:
                    _u = unwrap_tweet(_t)
                    _tid = _u.get("rest_id") or _u.get("legacy", {}).get("id_str")
                    if _tid and _tid not in _uniq:
                        _uniq[_tid] = _u
                extracted = list(_uniq.values())
                stats["extracted_raw"] += len(extracted)

                for t in extracted:
                    t = unwrap_tweet(t)
                    tid = t.get("rest_id") or t.get("legacy", {}).get("id_str")
                    if not tid:
                        continue

                    author = get_author_screen_name(t)
                    if author:
                        author_hist[author] = author_hist.get(author, 0) + 1
                    # Elite fix: old code dropped EVERYTHING when author=="" or
                    # when timeline contained retweets/replies from other authors.
                    # Keep all timeline tweets; author is kept for post-filtering.
                    if author and author.lower() != target_user.lower():
                        stats["filtered_out"] += 1
                        # still keep retweets/replies — they are part of the timeline
                        pass

                    if tid not in seen_ids:
                        seen_ids.add(tid)
                        tweets_data.append(t)
                        stats["kept"] += 1
                        if len(tweets_data) % 20 == 0:
                            log.info("  Intercepted %d tweets...", len(tweets_data))

                        if len(tweets_data) % CHECKPOINT_INTERVAL == 0:
                            checkpoint_file = save_json(
                                tweets_data, f"tweets_{target_user}_checkpoint.json"
                            )
                            log.info("Checkpoint saved: %s", checkpoint_file)
            except Exception as e:
                log.warning("handle_response error: %s", e)

        page.on("response", handle_response)

        log.info("Using authenticated session [%s]", "headless" if headless else "visible")
        log.info(
            "Target: @%s | Max: %d | Scroll rounds: %d", target_user, max_tweets, scroll_rounds
        )

        await page.goto(
            f"https://x.com/{target_user}", wait_until="domcontentloaded", timeout=60000
        )
        await asyncio.sleep(8)

        user_id = captured_user_id
        if not user_id:
            # Fallback: extract userId from page JS state
            try:
                user_id = await page.evaluate("""() => {
                    const m = document.documentElement.innerHTML.match(/"rest_id":"(\\d+)"/);
                    return m ? m[1] : null;
                }""")
            except Exception:
                user_id = None
        log.info(
            "User ID: %s (op=%s queryId=%s)",
            user_id,
            captured_timeline_op or "UserTweets?",
            captured_query_id or CURSOR_QUERY_ID,
        )

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

        async def collect_dom_batch() -> int:
            """Scrape visible <article> nodes into tweets_data. Returns # added."""
            try:
                dom_tweets = await page.evaluate("""() => {
                    const out = [];
                    document.querySelectorAll('article').forEach((a, idx) => {
                        const textEl = a.querySelector('div[lang]');
                        const timeEl = a.querySelector('time');
                        const links = [...a.querySelectorAll('a[href*="/status/"]')];
                        let id = null;
                        for (const l of links) {
                            const m = l.getAttribute('href').match(/\\/status\\/(\\d+)/);
                            if (m) { id = m[1]; break; }
                        }
                        out.push({
                            dom_id: id || `dom-${idx}`,
                            text: textEl ? textEl.innerText : a.innerText.slice(0, 500),
                            created_at: timeEl ? timeEl.getAttribute('datetime') : null,
                        });
                    });
                    return out;
                }""")
                added = 0
                for d in dom_tweets or []:
                    tid = d.get("dom_id")
                    if tid and tid not in seen_ids:
                        seen_ids.add(tid)
                        # Exporter-compatible: export_csv.py reads
                        # core.user_results.result.core.screen_name, config.py
                        # reads legacy.screen_name — provide BOTH.
                        tweets_data.append({
                            "rest_id": tid if not tid.startswith("dom-") else None,
                            "legacy": {
                                "id_str": tid,
                                "full_text": d.get("text", ""),
                                "created_at": d.get("created_at", ""),
                                "favorite_count": 0,
                                "retweet_count": 0,
                                "reply_count": 0,
                                "quote_count": 0,
                            },
                            "core": {"user_results": {"result": {
                                "legacy": {"screen_name": target_user},
                                "core": {"screen_name": target_user, "name": target_user},
                            }}},
                            "_dom_fallback": True,
                            "_target": target_user,
                        })
                        added += 1
                        if len(tweets_data) >= max_tweets:
                            break
                return added
            except Exception as e:
                log.warning("DOM batch failed: %s", e)
                return 0

        no_new_tweets_count = 0
        for i in range(scroll_rounds):
            prev_count = len(tweets_data)

            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            jitter = SCROLL_DELAY_BASE + (i % 3) * 0.7
            await asyncio.sleep(jitter)

            # Incremental DOM harvest: even if GraphQL parsing yields 0,
            # visible articles accumulate as we scroll (CREAWKenya case).
            dom_added = await collect_dom_batch()
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

            log.info("Scroll %d/%d — %d tweets (+%d dom)", i + 1, scroll_rounds, new_count, dom_added)

            if new_count >= max_tweets:
                log.info("Reached max tweets target")
                break

        if len(tweets_data) < max_tweets and next_cursor and user_id:
            query_id = captured_query_id or CURSOR_QUERY_ID
            timeline_op = captured_timeline_op or "UserTweets"
            log.info(
                "Switching to cursor pagination (%s queryId=%s cursor: %s...)",
                timeline_op,
                query_id,
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
                            const url = 'https://x.com/i/api/graphql/{query_id}/{timeline_op}';
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
                            const text = await res.text();
                            try {{ return JSON.parse(text); }}
                            catch (e) {{ return {{_http_status: res.status, _body_snippet: text.slice(0, 300)}}; }}
                        }}
                    """)

                    if not result or not result.get("data"):
                        log.warning("Cursor fetch returned empty data")
                        break

                    new_tweets = extract_tweets_from_response(result)
                    new_cursor = extract_cursor_from_response(result)

                    new_count = 0
                    for t in new_tweets:
                        t = unwrap_tweet(t)
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

        # Diagnostics (elite: never fail silently with "0 tweets")
        log.info(
            "Network stats: graphql_total=%d matched=%d extracted_raw=%d kept=%d filtered_out=%d",
            stats["graphql_total"],
            stats["matched"],
            stats["extracted_raw"],
            stats["kept"],
            stats["filtered_out"],
        )
        if author_hist:
            top = sorted(author_hist.items(), key=lambda x: -x[1])[:5]
            log.info("Author distribution: %s", top)

        # DOM fallback: even if GraphQL shapes change, articles are visible
        # (you saw "Found tweet articles" but 0 intercepted). Scrape DOM.
        # Incremental harvest already ran each scroll; this final pass catches
        # anything rendered after the last scroll.
        if len(tweets_data) < max_tweets:
            try:
                article_count = await page.locator("article").count()
                log.info("DOM fallback: %d <article> nodes visible", article_count)
                if article_count > 0:
                    added = await collect_dom_batch()
                    log.info("DOM fallback added %d tweets", added)
            except Exception as e:
                log.warning("DOM fallback failed: %s", e)

        log.info("Collected %d raw tweets", len(tweets_data))
        await context.close()

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
