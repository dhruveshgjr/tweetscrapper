"""
session_vault.py — Opt-in multi-session management with health checks.

Prevents single-session burn: register extra authenticated browser
profiles (created via 1_authenticator.py pointed at a different
USER_DATA_DIR), then rotate to the least-used healthy session.
Auto-cools flagged sessions instead of hammering them.

Single-session runs keep working untouched: 5_leviathan.py only
notifies the vault (mark_flagged) when it sees 401/403, and never
requires extra sessions to be registered.

Usage:
  python session_vault.py register main ./x_session
  python session_vault.py register backup ./x_session_2
  python session_vault.py list
"""

import argparse
import json
import time
from pathlib import Path

from config import log

SESSIONS_DIR = Path("./sessions")
SESSION_STATE_FILE = SESSIONS_DIR / "session_state.json"
COOLDOWN_SECONDS = 1800  # 30 min cooldown after a session gets flagged
MAX_SESSION_AGE_HOURS = 72  # re-authenticate sessions older than this


class SessionVault:
    def __init__(self):
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()

    def _load_state(self) -> dict:
        if SESSION_STATE_FILE.exists():
            try:
                with open(SESSION_STATE_FILE, "r") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "sessions" in data:
                    return data
            except Exception as e:
                log.warning("Vault state unreadable, starting fresh: %s", e)
        return {"sessions": {}}

    def _save_state(self):
        with open(SESSION_STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2)

    def register_session(self, name: str, user_data_dir: str):
        self.state["sessions"][name] = {
            "user_data_dir": user_data_dir,
            "created_at": time.time(),
            "flagged_until": 0,
            "request_count": 0,
            "status": "healthy",
        }
        self._save_state()
        log.info("Session '%s' registered", name)

    def get_active_session(self) -> dict | None:
        now = time.time()
        healthy = []
        for name, info in self.state["sessions"].items():
            if info.get("status") == "healthy" and now > info.get("flagged_until", 0):
                age_hours = (now - info.get("created_at", now)) / 3600
                if age_hours < MAX_SESSION_AGE_HOURS:
                    healthy.append((name, info))
        if not healthy:
            log.warning("No healthy sessions available")
            return None
        # Rotate: pick the one with fewest requests.
        healthy.sort(key=lambda x: x[1].get("request_count", 0))
        return {"name": healthy[0][0], **healthy[0][1]}

    def mark_flagged(self, session_dir: str, reason: str = "429"):
        for name, info in self.state["sessions"].items():
            if info.get("user_data_dir") == session_dir:
                info["flagged_until"] = time.time() + COOLDOWN_SECONDS
                info["status"] = "cooldown"
                log.warning(
                    "Session '%s' flagged (%s). Cooldown %ds", name, reason, COOLDOWN_SECONDS
                )
                break
        self._save_state()

    def mark_healthy(self, session_dir: str):
        for name, info in self.state["sessions"].items():
            if info.get("user_data_dir") == session_dir:
                info["status"] = "healthy"
                info["request_count"] = info.get("request_count", 0) + 1
                break
        self._save_state()

    def list_sessions(self) -> list:
        now = time.time()
        return [
            {"name": k, **v, "on_cooldown": now < v.get("flagged_until", 0)}
            for k, v in self.state["sessions"].items()
        ]


vault = SessionVault()


def main():
    ap = argparse.ArgumentParser(description="Manage authenticated X sessions")
    sub = ap.add_subparsers(dest="cmd", required=True)
    reg = sub.add_parser("register", help="Register an authenticated profile dir")
    reg.add_argument("name")
    reg.add_argument("user_data_dir")
    sub.add_parser("list", help="List sessions and cooldown state")
    a = ap.parse_args()
    if a.cmd == "register":
        vault.register_session(a.name, a.user_data_dir)
        print(f"Registered '{a.name}' -> {a.user_data_dir}")
    elif a.cmd == "list":
        for s in vault.list_sessions():
            flag = " (COOLDOWN)" if s["on_cooldown"] else ""
            print(f"{s['name']}: {s['user_data_dir']} [{s['status']}]{flag}")


if __name__ == "__main__":
    main()
