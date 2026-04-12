"""
1_authenticator.py
Log into X.com once, save authenticated browser state.
Run this ONCE manually. Then use the saved state for all scrapers.

Usage:
  python 1_authenticator.py
"""

import asyncio
import sys
from patchright.async_api import async_playwright

from config import USER_DATA_DIR, log


async def login_and_save_state():
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://x.com/login", wait_until="domcontentloaded")
        print("\n" + "=" * 50)
        print("  Log in to X.com in the browser window.")
        print("  After login is complete, come back here.")
        print("=" * 50 + "\n")
        input("Press Enter after you have logged in...")

        cookies = await context.cookies()
        ct0_found = any(c["name"] == "ct0" for c in cookies)
        auth_found = any(c["name"] == "auth_token" for c in cookies)

        if ct0_found and auth_found:
            log.info("Session saved to %s (ct0 + auth_token confirmed)", USER_DATA_DIR)
        else:
            log.warning(
                "Session saved but cookies may be incomplete (ct0=%s, auth_token=%s)",
                ct0_found,
                auth_found,
            )
            print("Warning: Login cookies may not have been captured. Re-run if scraping fails.")

        await context.close()


if __name__ == "__main__":
    try:
        asyncio.run(login_and_save_state())
    except KeyboardInterrupt:
        print("\nInterrupted. Session may not be fully saved.")
        sys.exit(1)
