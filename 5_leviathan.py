#!/usr/bin/env python3
"""
5_leviathan.py — PROJECT LEVIATHAN v2.1 (WAF Bypass + Dynamic Features)
Usage:
python 5_leviathan.py --handle CREAWKenya --start 2020-01-01 --end 2026-09-18
"""
import argparse
import asyncio
import datetime
import json
import random
import re
import sqlite3
import urllib.parse
from collections import deque
from pathlib import Path
from patchright.async_api import async_playwright
from config import USER_DATA_DIR, log, GRAPHQL_FEATURES

DB_PATH = Path("./output/leviathan.db")
SPLIT_THRESHOLD = 300
MAX_HARD_FAILS = 4

# Fallback if probe fails to capture the live features string
FALLBACK_FEATURES = json.dumps(GRAPHQL_FEATURES)

FETCH_JS = """
async ([queryId, rawQuery, cursor, featuresStr]) => {
    const vars = { rawQuery: rawQuery, count: 20, querySource: "typed_query", product: "Latest" };
    if (cursor) vars.cursor = cursor;
    
    // Decoded Bearer Token (crucial fix: = instead of %3D)
    const BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA";
    const ct0 = (document.cookie.match(/(?:^|;\\s*)ct0=([^;]+)/) || [])[1] || "";
    
    const url = "https://x.com/i/api/graphql/" + queryId + "/SearchTimeline?variables=" +
        encodeURIComponent(JSON.stringify(vars)) + "&features=" + encodeURIComponent(featuresStr);
        
    let res;
    try {
        res = await fetch(url, {
            credentials: "include",
            headers: {
                "authorization": "Bearer " + BEARER,
                "x-csrf-token": ct0,
                "x-twitter-active-user": "yes",
                "x-twitter-auth-type": "OAuth2Session",
                "x-twitter-client-language": "en",
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin"
            }
        });
    } catch (e) { return { status: -1, json: null }; }
    
    const text = await res.text();
    let json = null;
    try { json = JSON.parse(text); } catch (e) {}
    return { status: res.status, json: json };
}
"""

# --------------------------------------------------------------------------- DB
def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS tweets (
        tweet_id TEXT PRIMARY KEY, handle TEXT, created_at TEXT, full_text TEXT,
        likes INTEGER, retweets INTEGER, replies INTEGER, quotes INTEGER, views INTEGER,
        url TEXT, scraped_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS progress (
        handle TEXT, slice_start TEXT, slice_end TEXT, status TEXT, last_cursor TEXT,
        PRIMARY KEY (handle, slice_start))""")
    conn.commit()
    return conn

def save_tweet(conn, t, handle):
    legacy = t.get("legacy", {}) or {}
    core = ((t.get("core") or {}).get("user_results") or {}).get("result") or {}
    core = (core.get("core") or {})
    tid = t.get("rest_id") or legacy.get("id_str")
    if not tid: return False
    
    text = (legacy.get("full_text") or "").replace("\r", " ").replace("\n", " ").strip()
    sn = core.get("screen_name") or handle
    views = (t.get("views") or {}).get("count", 0)
    
    try:
        conn.execute("""INSERT OR IGNORE INTO tweets 
            (tweet_id, handle, created_at, full_text, likes, retweets, replies, quotes, views, url, scraped_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))""", 
            (tid, sn, legacy.get("created_at", ""), text,
             legacy.get("favorite_count", 0), legacy.get("retweet_count", 0),
             legacy.get("reply_count", 0), legacy.get("quote_count", 0),
             int(views) if str(views).isdigit() else 0,
             f"https://x.com/{sn}/status/{tid}"))
        return True
    except Exception as e:
        log.warning("DB insert error: %s", e)
        return False

def get_progress(conn, handle, slice_start):
    c = conn.cursor()
    c.execute("SELECT status FROM progress WHERE handle=? AND slice_start=?", (handle, slice_start))
    r = c.fetchone()
    return r[0] if r else None

def set_progress(conn, handle, s, e, status):
    conn.execute("INSERT OR REPLACE INTO progress (handle, slice_start, slice_end, status, last_cursor) VALUES (?,?,?,?,NULL)",
                 (handle, s, e, status))
    conn.commit()

# --------------------------------------------------------------------- parsing
def parse_search_response(data):
    tweets, cursor = [], None
    if not isinstance(data, dict): return tweets, cursor
    try:
        instructions = (data.get("data", {}).get("search_by_raw_query", {})
                        .get("search_timeline", {}).get("timeline", {}).get("instructions", []))
        for instr in instructions:
            for entry in instr.get("entries", []):
                content = entry.get("content", {}) or {}
                et = content.get("entryType")
                if et == "TimelineTimelineItem":
                    r = ((content.get("itemContent") or {}).get("tweet_results") or {}).get("result") or {}
                    if r.get("__typename") == "TweetWithVisibilityResults": r = r.get("tweet") or {}
                    if r: tweets.append(r)
                elif et == "TimelineTimelineCursor" and content.get("cursorType") == "Bottom":
                    cursor = content.get("value")
    except Exception as e:
        log.warning("Parse error: %s", e)
    return tweets, cursor

# --------------------------------------------------------------------- slices
def make_slices(start_str, end_str, days):
    start = datetime.datetime.strptime(start_str, "%Y-%m-%d").date()
    end = datetime.datetime.strptime(end_str, "%Y-%m-%d").date()
    out = deque()
    cur = start
    while cur < end:
        nxt = min(cur + datetime.timedelta(days=days), end)
        out.append((cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
        cur = nxt
    return out

def subdivide(s, e):
    d0 = datetime.datetime.strptime(s, "%Y-%m-%d").date()
    d1 = datetime.datetime.strptime(e, "%Y-%m-%d").date()
    out = []
    cur = d0
    while cur < d1:
        nxt = min(cur + datetime.timedelta(days=1), d1)
        out.append((cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
        cur = nxt
    return out

# --------------------------------------------------------------------- engine
async def probe_query_id(page, handle):
    """Captures QueryID AND the exact live features string from the probe URL."""
    state = {"query_id": None, "status": None, "features": None}
    
    def on_request(req):
        if "SearchTimeline" in req.url and not state["query_id"]:
            m = re.search(r"/graphql/([^/]+)/SearchTimeline", req.url)
            if m:
                state["query_id"] = m.group(1)
                parsed = urllib.parse.urlparse(req.url)
                qs = urllib.parse.parse_qs(parsed.query)
                if "features" in qs:
                    state["features"] = qs["features"][0]
                    
    async def on_response(resp):
        if "SearchTimeline" in resp.url and state["status"] is None:
            state["status"] = resp.status

    page.on("request", on_request)
    page.on("response", on_response)
    
    probe_q = urllib.parse.quote(f"from:{handle}")
    for attempt in range(3):
        log.info("🛰️  Probe %d/3: loading search page...", attempt + 1)
        try:
            await page.goto(f"https://x.com/search?q={probe_q}&src=typed_query&f=live",
                            wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log.warning("Probe navigation error: %s", e)
            
        for _ in range(25):
            if state["query_id"] and state["status"] is not None: break
            await asyncio.sleep(1)
            
        if state["query_id"]: break
        await asyncio.sleep(random.uniform(15, 30))
        
    page.remove_listener("request", on_request)
    page.remove_listener("response", on_response)
    
    if not state["query_id"]:
        log.error("❌ No SearchTimeline request fired. Re-run 1_authenticator.py")
        return None, None, None
        
    log.info("🛰️  Live QueryID: %s | probe HTTP status: %s", state["query_id"], state["status"])
    if state["status"] in (401, 403):
        log.error("❌ Search returned %s — session blocked. Re-run 1_authenticator.py", state["status"])
        return state["query_id"], state["status"], None
        
    return state["query_id"], state["status"], state["features"]

async def fetch_page(page, query_id, raw_query, cursor, features_str):
    try:
        return await page.evaluate(FETCH_JS, [query_id, raw_query, cursor, features_str])
    except Exception as e:
        log.warning("evaluate error: %s", e)
        return {"status": -1, "json": None}

async def scrape_slice(page, conn, handle, query_id, s, e, fail_streak, features_str):
    raw = f"from:{handle} since:{s} until:{e}"
    cursor = None
    added = 0
    pages = 0
    
    while True:
        pages += 1
        result = await fetch_page(page, query_id, raw, cursor, features_str)
        status = result.get("status")
        data = result.get("json")
        
        if status != 200 or not data:
            fail_streak += 1
            if status == 429:
                wait = random.uniform(60, 150)
                log.warning("⏳ 429 rate-limit. Backoff %.0fs (streak %d)", wait, fail_streak)
                await asyncio.sleep(wait)
                if fail_streak >= MAX_HARD_FAILS: return None, fail_streak, added
                continue
            
            wait = random.uniform(20, 45)
            log.warning("⚠️ HTTP %s on %s. Retry in %.0fs (streak %d)", status, s, wait, fail_streak)
            await asyncio.sleep(wait)
            if fail_streak >= MAX_HARD_FAILS: return None, fail_streak, added
            continue
            
        fail_streak = 0
        tweets, cursor = parse_search_response(data)
        for t in tweets:
            if save_tweet(conn, t, handle): added += 1
        conn.commit()
        
        if pages % 5 == 0 or not cursor:
            log.info("   slice %s→%s page %d: +%d (slice total %d)", s, e, pages, len(tweets), added)
            
        if not cursor: break
        await asyncio.sleep(random.uniform(2.0, 5.0))
        
    return True, fail_streak, added

async def inject_noise(context):
    try:
        log.info("😈 Noise injection (doomscroll home)...")
        page = await context.new_page()
        await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(8, 15))
        for _ in range(random.randint(2, 5)):
            await page.evaluate("window.scrollBy(0, 600)")
            await asyncio.sleep(random.uniform(1.5, 3.0))
        await page.close()
    except Exception as e:
        log.warning("Noise failed: %s", e)

async def run(handle, start, end, db_path, headless, noise_interval, slice_days):
    conn = init_db()
    queue = make_slices(start, end, slice_days)
    log.info("🚀 LEVIATHAN v2.1: @%s | %d initial slices | WAF Bypass Active", handle, len(queue))
    
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR, headless=headless, viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"])
        page = context.pages[0] if context.pages else await context.new_page()
        
        await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(4)
        
        query_id, probe_status, features_str = await probe_query_id(page, handle)
        if not query_id or probe_status in (401, 403):
            await context.close(); conn.close(); return
            
        if not features_str:
            log.warning("⚠️ Could not capture live features string. Using fallback.")
            features_str = FALLBACK_FEATURES
            
        await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        
        done_count = 0
        fail_streak = 0
        aborted = False
        
        while queue:
            s, e = queue.popleft()
            if get_progress(conn, handle, s) in ("DONE", "SPLIT"): continue
            
            done_count += 1
            log.info("🔥 Slice %s → %s (queue left: %d)", s, e, len(queue))
            
            ok, fail_streak, added = await scrape_slice(page, conn, handle, query_id, s, e, fail_streak, features_str)
            
            if ok is None:
                aborted = True
                log.error("🛑 ABORT: too many consecutive failures. Progress saved.")
                break
                
            if added >= SPLIT_THRESHOLD:
                log.info("✂️  Week %s had %d tweets — subdividing into daily slices", s, added)
                for sub in reversed(subdivide(s, e)): queue.appendleft(sub)
                set_progress(conn, handle, s, e, "SPLIT")
            else:
                set_progress(conn, handle, s, e, "DONE")
                log.info("✅ Slice %s done: %d tweets", s, added)
                
            if done_count % noise_interval == 0: await inject_noise(context)
            if done_count % 50 == 0:
                wait = random.uniform(120, 300)
                log.info("☕ Long human break %.0fs", wait)
                await asyncio.sleep(wait)
                
            await asyncio.sleep(random.uniform(1.5, 4.0))
            
        await context.close()
        
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM tweets WHERE handle=?", (handle,))
    total = c.fetchone()[0]
    conn.close()
    log.info("🏆 Run %s. DB total for @%s: %d tweets", "ABORTED (resumable)" if aborted else "COMPLETE", handle, total)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--handle", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--db", default="./output/leviathan.db")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--noise-interval", type=int, default=15)
    ap.add_argument("--slice-days", type=int, default=7)
    a = ap.parse_args()
    asyncio.run(run(a.handle, a.start, a.end, Path(a.db), a.headless, a.noise_interval, a.slice_days))

if __name__ == "__main__":
    main()