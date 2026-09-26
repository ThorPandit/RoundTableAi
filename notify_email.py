"""
notify_email.py — Send notifications and get replies via Email (Gmail).

Drop-in replacement for notify.py with the same interface:
    enabled(), send(text), ask(question), ask_yes_no(question)

Setup (one time, ~5 minutes):

1. Enable IMAP in Gmail:
   Gmail → Settings → See all settings → Forwarding and POP/IMAP
   → Enable IMAP → Save Changes

2. Turn on 2-Step Verification:
   https://myaccount.google.com/security → 2-Step Verification → turn on

3. Create an App Password:
   https://myaccount.google.com/apppasswords
   → Select app: Mail, Select device: Windows Computer
   → Generate → copy the 16-character password (looks like "abcd efgh ijkl mnop")
   → Remove spaces: "abcdefghijklmnop"

4. Add these to your .env:
   EMAIL_USER=yourname@gmail.com
   EMAIL_APP_PASSWORD=abcdefghijklmnop
   EMAIL_TO=yourname@gmail.com

Optional overrides (defaults shown):
   EMAIL_SMTP_HOST=smtp.gmail.com
   EMAIL_SMTP_PORT=587
   EMAIL_IMAP_HOST=imap.gmail.com
   EMAIL_IMAP_PORT=993
"""

import os
import re
import time
import ssl
import email
import smtplib
import imaplib
import random
import string
from email.mime.text import MIMEText
from email.header import decode_header

# ============================================================
# FORCE IPv4 — corporate networks often filter IPv6
# ============================================================
import socket
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_only_getaddrinfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ============================================================
# Config from .env
# ============================================================
SMTP_HOST = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = int(os.getenv("EMAIL_SMTP_PORT", "587").strip())
IMAP_HOST = os.getenv("EMAIL_IMAP_HOST", "imap.gmail.com").strip()
IMAP_PORT = int(os.getenv("EMAIL_IMAP_PORT", "993").strip())

USER = os.getenv("EMAIL_USER", "").strip()
PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "").strip().replace(" ", "")
TO = os.getenv("EMAIL_TO", "").strip() or USER

# Marker used to identify our messages in the inbox
MARKER = "LocalMind"


def enabled():
    return bool(USER and PASSWORD and TO)


# ============================================================
# SMTP — send
# ============================================================
def _smtp_send(subject, body, reply_to_msg_id=None):
    """Low-level send. Returns Message-ID string or None on failure."""
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = USER
        msg["To"] = TO
        msg["Subject"] = subject
        if reply_to_msg_id:
            msg["In-Reply-To"] = reply_to_msg_id
            msg["References"] = reply_to_msg_id

        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.ehlo()
            s.starttls(context=context)
            s.ehlo()
            s.login(USER, PASSWORD)
            s.send_message(msg)
        return msg["Message-ID"] or "sent"
    except Exception as e:
        print(f"[notify-email] send failed: {type(e).__name__}: {e}")
        return None


def send(text):
    """Send a one-way notification. Returns True on success."""
    if not enabled():
        return False
    subject = f"[{MARKER}] Notification"
    return _smtp_send(subject, text[:5000]) is not None


# ============================================================
# IMAP — receive
# ============================================================
def _decode_header(value):
    """Decode a possibly-encoded email header into a plain string."""
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="replace"))
            except Exception:
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _get_body(msg):
    """Extract plain text body from an email.message.Message."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in disp:
                try:
                    return part.get_payload(decode=True).decode("utf-8", errors="replace")
                except Exception:
                    continue
        # Fallback to HTML, stripped
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_payload(decode=True).decode("utf-8", errors="replace")
                    return re.sub(r"<[^>]+>", "", html)
                except Exception:
                    continue
    else:
        try:
            return msg.get_payload(decode=True).decode("utf-8", errors="replace")
        except Exception:
            return str(msg.get_payload())
    return ""


def _strip_quoted(body):
    """Remove quoted text and signatures from a reply."""
    if not body:
        return ""
    lines = []
    for line in body.splitlines():
        s = line.strip()
        # Stop at common quote markers
        if s.startswith(">"):
            break
        if re.match(r"^On .+wrote:$", s):
            break
        if re.match(r"^-{2,}\s*Original Message\s*-{2,}", s, re.IGNORECASE):
            break
        if re.match(r"^_{5,}$", s):
            break
        if s.startswith("-- "):  # signature separator
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _check_reply(token, timeout_seconds):
    """Poll IMAP for a reply containing our token in the subject."""
    deadline = time.time() + timeout_seconds
    target_subject = f"[{MARKER}-{token}]"

    while time.time() < deadline:
        try:
            context = ssl.create_default_context()
            with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=context) as m:
                m.login(USER, PASSWORD)
                m.select("INBOX")
                # Search for unread messages since yesterday with the marker
                typ, data = m.search(None, '(UNSEEN SUBJECT "LocalMind")')
                if typ == "OK" and data and data[0]:
                    msg_ids = data[0].split()
                    # Newest first
                    for mid in reversed(msg_ids):
                        typ, msg_data = m.fetch(mid, "(RFC822)")
                        if typ != "OK" or not msg_data or not msg_data[0]:
                            continue
                        raw = msg_data[0][1]
                        msg = email.message_from_bytes(raw)
                        subject = _decode_header(msg.get("Subject", ""))
                        # Match either the exact token or any LocalMind reply
                        if target_subject in subject or f"[{MARKER}" in subject:
                            body = _strip_quoted(_get_body(msg))
                            # Mark as read so we don't process again
                            try:
                                m.store(mid, "+FLAGS", "\\Seen")
                            except Exception:
                                pass
                            if body:
                                return body
        except Exception as e:
            print(f"[notify-email] poll error: {type(e).__name__}: {e}")
        time.sleep(5)  # poll every 5 seconds

    return None


# ============================================================
# High-level ask
# ============================================================
def ask(question, timeout_seconds=600):
    """Send a question, wait for a reply email. Returns text or None."""
    if not enabled():
        return None

    # Generate a unique token so replies can be matched to this question
    token = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    subject = f"[{MARKER}-{token}] Question from Local Mind"

    body = (
        f"{question}\n\n"
        f"---\n"
        f"Reply to this email. Your reply (above the quoted text) will be "
        f"sent back to the agent.\n"
        f"Token: {token}\n"
    )

    if _smtp_send(subject, body) is None:
        return None

    print(f"[notify-email] question sent (token {token}). Waiting up to "
          f"{timeout_seconds}s for reply...")

    reply = _check_reply(token, timeout_seconds)
    if reply is None:
        send("⌛ Timed out waiting for your reply to the last question.")
    return reply


def ask_yes_no(question, timeout_seconds=600):
    """Ask yes/no. Returns True / False / None (timeout)."""
    answer = ask(f"{question}\n\nReply *yes* or *no*.", timeout_seconds=timeout_seconds)
    if answer is None:
        return None
    a = answer.strip().lower()
    # Only look at the first word
    first = a.split()[0] if a.split() else ""
    if first in ("yes", "y", "ok", "okay", "sure", "go", "approve", "approved", "1"):
        return True
    if first in ("no", "n", "stop", "cancel", "deny", "denied", "0"):
        return False
    # If they wrote a longer sentence, look for yes/no anywhere
    if re.search(r"\byes\b", a) or re.search(r"\bapprove", a):
        return True
    if re.search(r"\bno\b", a) or re.search(r"\bdeny\b", a) or re.search(r"\bcancel\b", a):
        return False
    return None


# ============================================================
# Self-test
# ============================================================
if __name__ == "__main__":
    if not enabled():
        print("Email not configured.")
        print("Add EMAIL_USER, EMAIL_APP_PASSWORD, EMAIL_TO to your .env file.")
        print("See the docstring at the top of notify_email.py for setup steps.")
    else:
        print(f"From: {USER}")
        print(f"To:   {TO}")
        print(f"SMTP: {SMTP_HOST}:{SMTP_PORT}")
        print(f"IMAP: {IMAP_HOST}:{IMAP_PORT}")
        print("\nTesting send...")
        if send("🔔 Test from Local Mind (email fallback)"):
            print("✅ Send works.")
        else:
            print("❌ Send failed.")
            print("Check App Password and that 2FA is on.")
            raise SystemExit(1)

        print("\nTesting ask (120s timeout) — check your inbox and reply now...")
        ans = ask("This is a test. Reply with anything.", timeout_seconds=120)
        print(f"Reply received: {ans!r}")