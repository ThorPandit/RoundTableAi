"""
notify.py — Send notifications and get replies via Telegram.

Setup (one time, 5 minutes):
1. Open Telegram, search for @BotFather, start a chat.
2. Send: /newbot
3. Give it a name (e.g. "LocalMindBot") and a username ending in "bot" (e.g. "shubham_mind_bot").
4. BotFather replies with a token like: 123456789:AAH...xyz
5. IMPORTANT: open a chat with YOUR new bot and send it any message (e.g. "hi").
6. Then visit in browser:
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   Find "chat":{"id":123456789,...} — that number is your CHAT_ID.
7. Add both to your .env file:
   TELEGRAM_TOKEN=123456789:AAH...xyz
   TELEGRAM_CHAT_ID=123456789
"""

import os
import time
import socket
import requests

# ============================================================
# FORCE IPv4 — needed on corporate networks that block IPv6
# ============================================================
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_only_getaddrinfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BASE = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""


def enabled():
    return bool(TOKEN and CHAT_ID)


def send(text):
    """Send a one-way notification. Returns True on success."""
    if not enabled():
        return False
    try:
        r = requests.post(
            f"{BASE}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text[:4000], "parse_mode": "Markdown"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[notify] send failed: {e}")
        return False


def _latest_offset():
    """Return highest update_id + 1 so we only see new messages."""
    try:
        r = requests.get(f"{BASE}/getUpdates", params={"timeout": 0}, timeout=10)
        if r.status_code == 200:
            updates = r.json().get("result", [])
            if updates:
                return updates[-1]["update_id"] + 1
    except Exception:
        pass
    return 0


def ask(question, timeout_seconds=300, poll_interval=3):
    """Send a question, wait for reply. Returns text or None on timeout."""
    if not enabled():
        return None

    offset = _latest_offset()
    ok = send(f"❓ *Local Mind asks:*\n\n{question}\n\n_Reply to this bot._")
    if not ok:
        return None

    print(f"[notify] waiting up to {timeout_seconds}s for reply on Telegram...")
    start = time.time()
    while time.time() - start < timeout_seconds:
        try:
            r = requests.get(
                f"{BASE}/getUpdates",
                params={"offset": offset, "timeout": 5},
                timeout=20,
            )
            if r.status_code == 200:
                for u in r.json().get("result", []):
                    offset = u["update_id"] + 1
                    msg = u.get("message") or u.get("edited_message")
                    if not msg:
                        continue
                    if str(msg.get("chat", {}).get("id")) != CHAT_ID:
                        continue
                    text = (msg.get("text") or "").strip()
                    if text:
                        send(f"✅ Received: _{text}_")
                        return text
        except Exception as e:
            print(f"[notify] poll error: {e}")
        time.sleep(poll_interval)

    send("⌛ Timed out waiting for reply.")
    return None


def ask_yes_no(question, timeout_seconds=300):
    """Ask a yes/no question. Returns True / False / None (timeout)."""
    answer = ask(f"{question}\n\nReply *yes* or *no*.", timeout_seconds=timeout_seconds)
    if answer is None:
        return None
    a = answer.strip().lower()
    if a in ("yes", "y", "ok", "okay", "sure", "go", "approve", "approved", "1"):
        return True
    if a in ("no", "n", "stop", "cancel", "deny", "denied", "0"):
        return False
    return None


if __name__ == "__main__":
    if not enabled():
        print("Telegram not configured.")
        print("Add TELEGRAM_TOKEN and TELEGRAM_CHAT_ID to your .env file.")
        print("See the docstring at the top of notify.py for setup steps.")
    else:
        print("Testing send...")
        if send("🔔 Test from Local Mind"):
            print("✅ Send works.")
        else:
            print("❌ Send failed.")
        print("\nTesting ask (60s timeout) — reply on Telegram now...")
        ans = ask("This is a test. Reply anything.", timeout_seconds=60)
        print(f"Reply received: {ans!r}")