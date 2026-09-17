#!/usr/bin/env python3
"""
5_leviathan.py — PROJECT LEVIATHAN v3 (NAV-INTERCEPT ENGINE)
Browser-native search navigation + response interception.
No synthetic fetch() — X kills those with 404 (x-client-transaction-id).
SQLite resume + weekly->daily auto-split + anti-ban pacing.
Usage:
  python 5_leviathan.py --handle CREAWKenya --start 2020-01-01 --end 2026-09-18 --headless
"""
import argparse
import asyncio
import datetime
import random
import sqlite3
import urllib.parse
from collections import deque
from pathlib import Path

from patchright.async_api import async_playwright

from config import (
    BROWSER_LOCALE,
    BROWSER_TIMEZONE,
    USER_DATA_DIR,
    cooldown_delay,
    human_delay,
    log,
    page_load_delay,
    scroll_delay,
)

try:
    from session_vault import vault as _vault
except Exception:
    _vault = None


def _vault_flagged(session_dir: str, reason: str):
    if _vault is None:
        return
    try:
        _vault.mark_flagged(session_dir, reason=reason)
    except Exception as e:
        log.warning("Vault flag failed: %s", e)

DB_PATH = Path("./output/leviathan.db")
SPLIT_THRESHOLD = 300
MAX_SCROLLS = 25
MAX_SLICE_RETRIES = 3
MAX_CONSEC_FAILS = 8
RL_BASE_COOLDOWN = 900    # 15 min — matches X's search rate window
RL_MAX_COOLDOWN = 7200    # 2h cap
RL_MAX_STREAK = 5         # ~5 escalating windows, then clean resumable stop


# --------------------------------------------------------------------------- DB
def init_db(db_path=DB_PATH):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
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


def normalize_text_ws(text: str) -> str:
    if not text:
        return ""
    return text.replace("\r", " ").replace("\n", " ").strip()


def extract_full_text(t: dict) -> str:
    """Full tweet text, prioritizing note_tweet (long-form posts).

    Path 1: note_tweet shapes (X Articles / long-form) — direct text,
      nested note_tweet_results.result.text, or core.text.
    Path 2: extended_tweet.full_text (older long-form format).
    Path 3: legacy.full_text (standard tweets).
    """
    try:
        note = t.get("note_tweet", {})
        if isinstance(note, dict):
            if note.get("text"):
                return normalize_text_ws(note["text"])
            result = (note.get("note_tweet_results") or {}).get("result", {}) or {}
            if isinstance(result, dict) and result.get("text"):
                return normalize_text_ws(result["text"])
            core_text = (note.get("core") or {}).get("text")
            if core_text:
                return normalize_text_ws(core_text)
    except Exception:
        pass

    try:
        ext = t.get("extended_tweet", {}) or {}
        if isinstance(ext, dict) and ext.get("full_text"):
            return normalize_text_ws(ext["full_text"])
    except Exception:
        pass

    legacy = t.get("legacy", {}) or {}
    return normalize_text_ws(legacy.get("full_text", ""))


def save_tweet(conn, t, handle):
    legacy = t.get("legacy", {}) or {}
    user_res = ((t.get("core") or {}).get("user_results") or {}).get("result") or {}
    core = user_res.get("core") or {}
    tid = t.get("rest_id") or legacy.get("id_str")
    if not tid:
        return False
    text = extract_full_text(t)
    sn = core.get("screen_name") or handle
    views = (t.get("views") or {}).get("count", 0)
    try:
        views_int = int(views) if str(views).isdigit() else 0
    except (TypeError, ValueError):
        views_int = 0
    try:
        cur = conn.execute("""INSERT OR IGNORE INTO tweets
            (tweet_id, handle, created_at, full_text, likes, retweets, replies, quotes, views, url, scraped_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
            (tid, sn, legacy.get("created_at", ""), text,
             legacy.get("favorite_count", 0) or 0, legacy.get("retweet_count", 0) or 0,
             legacy.get("reply_count", 0) or 0, legacy.get("quote_count", 0) or 0,
             views_int,
             f"https://x.com/{sn}/status/{tid}"))
        return cur.rowcount > 0
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
def _unwrap_tweet(r):
    if not isinstance(r, dict) or not r:
        return None
    if r.get("__typename") == "TweetWithVisibilityResults":
        r = r.get("tweet") or {}
    if r.get("__typename") not in (None, "Tweet"):
        # Keep Tweetژه entries, drop users/cursors masquerading as tweets
        if r.get("__typename") != "Tweet":
            return None
    if not (r.get("rest_id") or (r.get("legacy") or {}).get("id_str")):
        return None
    return r


def parse_search_response(data):
    tweets, cursor = [], None
    if not isinstance(data, dict):
        return tweets, cursor
    try:
        instructions = (data.get("data", {}).get("search_by_raw_query", {})
                        .get("search_timeline", {}).get("timeline", {}).get("instructions", []))
        for instr in instructions:
            for entry in instr.get("entries", []):
                content = entry.get("content", {}) or {}
                et = content.get("entryType")
                if et == "TimelineTimelineItem":
                    r = ((content.get("itemContent") or {}).get("tweet_results") or {}).get("result") or {}
                    r = _unwrap_tweet(r)
                    if r:
                        tweets.append(r)
                elif et == "TimelineTimelineModule":
                    for item in content.get("items", []) or []:
                        ic = (item.get("item") or {}).get("itemContent") or {}
                        r = (ic.get("tweet_results") or {}).get("result") or {}
                        r = _unwrap_tweet(r)
                        if r:
                            tweets.append(r)
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
async def probe_search(page, handle):
    """Verify search works with NATIVE navigation. No QueryID needed."""
    for attempt in range(3):
        state = {"status": None}

        async def on_resp(resp):
            if "SearchTimeline" in resp.url and state["status"] is None:
                state["status"] = resp.status

        page.on("response", on_resp)
        try:
            await page.goto(f"https://x.com/search?q={urllib.parse.quote(f'from:{handle}')}&src=typed_query&f=live",
                            wait_until="domcontentloaded", timeout=60000)
        except Exception as ex:
            log.warning("Probe nav error: %s", ex)
        for _ in range(20):
            if state["status"] is not None:
                break
            await asyncio.sleep(1)
        try:
            page.remove_listener("response", on_resp)
        except Exception:
            pass
        log.info("Probe %d/3: SearchTimeline HTTP %s", attempt + 1, state["status"])
        if state["status"] == 200:
            return True
        if state["status"] == 429:
            wait = cooldown_delay(attempt) + 180  # probe-level: long cool
            log.warning("Probe rate-limited. Cooling %.0fs...", wait)
            await asyncio.sleep(wait)
            continue
        if state["status"] in (401, 403):
            log.error("Session search-blocked/logged out. Re-run 1_authenticator.py")
            _vault_flagged(USER_DATA_DIR, reason=f"HTTP_{state['status']}")
            return False
        await asyncio.sleep(human_delay(base=45, spread=1.0))
    return False


async def scrape_slice_nav(page, conn, handle, s, e):
    """Navigate natively, intercept SearchTimeline payloads, scroll to paginate."""
    raw = f"from:{handle} since:{s} until:{e}"
    url = f"https://x.com/search?q={urllib.parse.quote(raw)}&src=typed_query&f=live"
    state = {"payloads": 0, "tweets": [], "status": None, "seen_ids": set()}

    async def on_response(resp):
        if "SearchTimeline" not in resp.url:
            return
        if state["status"] is None:
            state["status"] = resp.status

        # SESSION HEALTH: fail fast on silent auth loss / rate limits.
        if resp.status in (401, 403):
            log.error("SESSION AUTH FAILURE (HTTP %d). Flagging session.", resp.status)
            _vault_flagged(USER_DATA_DIR, reason=f"HTTP_{resp.status}")
            return
        if resp.status == 429:
            log.warning("Rate limited on slice %s->%s", s, e)
            return

        if resp.status != 200:
            return
        try:
            data = await resp.json()
        except Exception:
            return
        tweets, _c = parse_search_response(data)
        state["payloads"] += 1
        for t in tweets:
            tid = t.get("rest_id") or (t.get("legacy") or {}).get("id_str")
            if tid and tid not in state["seen_ids"]:
                state["seen_ids"].add(tid)
                state["tweets"].append(t)

    page.on("response", on_response)
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as ex:
            log.warning("Nav error %s: %s", s, ex)
        for _ in range(20):
            if state["payloads"] or (state["status"] not in (None, 200)):
                break
            await asyncio.sleep(1)
        if state["status"] == 429:
            return "RATE_LIMITED", 0
        if state["status"] in (401, 403):
            return "SESSION_FLAGGED", 0
        if state["payloads"] == 0:
            return "NO_PAYLOAD", 0
        seen = len(state["tweets"])
        stale = 0
        for i in range(MAX_SCROLLS):
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception as ex:
                log.warning("Scroll error %s: %s", s, ex)
                break
            await asyncio.sleep(scroll_delay(i))
            now = len(state["tweets"])
            if now == seen:
                stale += 1
                if stale >= 3:
                    break
            else:
                stale = 0
                seen = now
            if i % 5 == 4:
                log.info("   slice %s->%s scroll %d/%d: %d tweets", s, e, i + 1, MAX_SCROLLS, now)
        return "OK", len(state["tweets"])
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        added = 0
        for t in state["tweets"]:
            if save_tweet(conn, t, handle):
                added += 1
        conn.commit()
        # Stash exact new-row count for the caller (dedupe-safe).
        scrape_slice_nav._last_added = added


async def inject_noise(context):
    try:
        log.info("Noise injection (doomscroll home)...")
        page = await context.new_page()
        await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(page_load_delay())
        for _ in range(random.randint(2, 5)):
            await page.evaluate("window.scrollBy(0, 600)")
            await asyncio.sleep(human_delay(base=2.2, spread=1.0))
        await page.close()
    except Exception as e:
        log.warning("Noise failed: %s", e)


async def run(handle, start, end, headless, noise_interval, slice_days, db_path=DB_PATH):
    handle = handle.lstrip("@")
    conn = init_db(db_path)
    queue = make_slices(start, end, slice_days)
    log.info("LEVIATHAN v3 NAV-INTERCEPT: @%s | %d slices | browser-native", handle, len(queue))
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR, headless=headless, viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox",
                  "--disable-dev-shm-usage",
                  "--disable-infobars"],
            timezone_id=BROWSER_TIMEZONE,
            locale=BROWSER_LOCALE)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(page_load_delay())
        if not await probe_search(page, handle):
            await context.close()
            conn.close()
            return
        done_count = 0
        consec = 0
        retries = {}
        aborted = False
        rl_streak = 0          # consecutive 429 windows (session heat)
        pace_multiplier = 1.0  # adaptive inter-slice pacing after heat
        while queue:
            s, e = queue.popleft()
            if get_progress(conn, handle, s) in ("DONE", "SPLIT"):
                continue
            done_count += 1
            log.info("Slice %s -> %s (queue left: %d)", s, e, len(queue))
            scrape_slice_nav._last_added = 0
            status, intercepted = await scrape_slice_nav(page, conn, handle, s, e)
            added = getattr(scrape_slice_nav, "_last_added", 0)
            # SPLIT decision uses intercepted volume (includes re-seen rows),
            # progress uses durable new-row count. Log both.
            if status == "OK":
                consec = 0
                rl_streak = 0
                pace_multiplier = max(1.0, pace_multiplier * 0.9)
                if intercepted >= SPLIT_THRESHOLD:
                    log.info("Week %s = %d tweets -> daily sub-slices", s, intercepted)
                    for sub in reversed(subdivide(s, e)):
                        queue.appendleft(sub)
                    set_progress(conn, handle, s, e, "SPLIT")
                else:
                    set_progress(conn, handle, s, e, "DONE")
                    log.info("Slice %s done: +%d new (%d intercepted)", s, added, intercepted)
            else:
                # ---- CIRCUIT BREAKER: 429 = session HOT, not slice bad ----
                if status == "RATE_LIMITED":
                    rl_streak += 1
                    wait = min(RL_MAX_COOLDOWN,
                               RL_BASE_COOLDOWN * (2 ** (rl_streak - 1)))
                    wait *= random.uniform(0.9, 1.3)
                    pace_multiplier = min(4.0, pace_multiplier * 1.5)
                    log.warning(
                        "429 on %s (rl_streak=%d). Session HOT — FULL-QUEUE PAUSE "
                        "%.0fs. Same slice retries first after cooldown.",
                        s, rl_streak, wait)
                    queue.appendleft((s, e))   # hold position; do NOT rotate
                    await asyncio.sleep(wait)
                    if rl_streak >= RL_MAX_STREAK:
                        aborted = True
                        log.error("RATE-LIMIT BREAKER: %d consecutive 429 windows. "
                                  "Stopping resumable — resume in a few hours.",
                                  rl_streak)
                        break
                    if not await probe_search(page, handle):
                        aborted = True
                        log.error("Probe still blocked after cooldown. "
                                  "Stopping resumable — session needs hours.")
                        break
                    continue  # skip noise/break/inter-slice sleeps; we just cooled
                # ---- non-429 failures: original semantics ----
                consec += 1
                retries[(s, e)] = retries.get((s, e), 0) + 1
                if status == "SESSION_FLAGGED":
                    wait = cooldown_delay(retries[(s, e)] + 1)
                    log.error("401/403 on %s — session dying. Cooling %.0fs. "
                              "Re-run 1_authenticator.py if this repeats.", s, wait)
                else:
                    wait = random.uniform(45, 90)
                    log.warning("%s on %s. Re-queue, backoff %.0fs (try %d/%d)",
                                status, s, wait, retries[(s, e)], MAX_SLICE_RETRIES)
                await asyncio.sleep(wait)
                if retries[(s, e)] <= MAX_SLICE_RETRIES:
                    queue.append((s, e))
                else:
                    log.error("Slice %s failed %dx — left un-DONE for next run "
                              "(resume-safe).", s, MAX_SLICE_RETRIES)
                if consec >= MAX_CONSEC_FAILS:
                    aborted = True
                    log.error("ABORT: %d consecutive failures. Progress saved.", consec)
                    break
            if done_count % noise_interval == 0:
                await inject_noise(context)
                await asyncio.sleep(max(5.0, human_delay(base=20, spread=1.0)))
            if done_count % 50 == 0:
                wait = max(60.0, human_delay(base=180, spread=1.2))
                log.info("Long human break %.0fs", wait)
                await asyncio.sleep(wait)
            await asyncio.sleep(human_delay(base=3.5, spread=1.2) * pace_multiplier)
        await context.close()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM tweets WHERE handle=?", (handle,))
    total = c.fetchone()[0]
    conn.close()
    log.info("Run %s. DB total for @%s: %d tweets",
             "ABORTED (resumable)" if aborted else "COMPLETE", handle, total)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--handle", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--noise-interval", type=int, default=8)
    ap.add_argument("--slice-days", type=int, default=7)
    a = ap.parse_args()
    asyncio.run(run(a.handle, a.start, a.end, a.headless, a.noise_interval, a.slice_days, Path(a.db)))


if __name__ == "__main__":
    main()
