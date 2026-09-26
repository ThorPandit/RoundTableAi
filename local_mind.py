"""
local_mind.py — Local autonomous agent with vision, UI automation, voice, memory,
auto-learning, multi-task queue, roundtable integration, background watcher,
and mobile (Telegram) notifications.

Layers:
  MIND    — Ollama (local LLM)
  EYES    — screenshot + moondream + Windows UIA
  HANDS   — pyautogui + subprocess + UIA control actions
  VOICE   — pyttsx3 TTS + faster-whisper STT
  MEMORY  — memory.md + agent_memory.json (RAG) + auto-learned rules
  MOBILE  — Telegram notifications + two-way approvals (away mode)

Run:
    python local_mind.py              # text REPL
    python local_mind.py --voice      # push-to-talk voice mode
    python local_mind.py --task "..." # one-shot
"""

import os
import io
import re
import sys
import json
import time
import math
import base64
import argparse
import sqlite3
import subprocess
import threading
from pathlib import Path

import requests
import pyautogui
import mss
from PIL import Image

import notify

# ============================================================
# PATHS
# ============================================================
LOG_PATH        = Path("agent_log.jsonl")
MEMORY_PATH     = Path("memory.md")
RAG_PATH        = Path("agent_memory.json")
ROUNDTABLE_DB   = Path(r"C:\Users\106627\ai-roundtable\roundtable.db")

# ============================================================
# Config
# ============================================================
OLLAMA_URL       = "http://127.0.0.1:11434"
MIND_MODEL       = "llama3.1:8b"
VISION_MODEL     = "moondream"
EMBED_MODEL      = "nomic-embed-text"
WHISPER_SIZE     = "base"
MAX_STEPS        = 14
STEP_DELAY       = 0.6
SCREEN_MAX_W     = 1280
SAFE_MODE        = True
SHELL_TIMEOUT    = 15
RAG_TOP_K        = 3
RAG_MIN_SCORE    = 0.55
AUTO_LEARN       = True
AWAY_MODE        = False         # route confirmations & reports to Telegram
WATCH_INTERVAL   = 30
WATCH_PROMPT     = (
    "Is there anything on this screen that needs the user's attention right now? "
    "Examples: an error dialog, a meeting reminder, a download finished, "
    "a suspicious popup, a crashed app. If nothing notable, answer exactly: NOTHING."
)

SAFE_COMMAND_PREFIXES = (
    "echo ", "dir ", "type ", "cd ", "pwd ",
    "where ", "whoami", "date", "time", "ver", "hostname",
    "tasklist", "ipconfig", "ping -n", "ping 127",
    "mkdir ", "md ", "copy ", "xcopy ", "move ",
    "start ", "notepad", "calc", "mspaint", "explorer",
    "cls", "set ",
)


# ============================================================
# LOGGING
# ============================================================
def log_event(event_type, **data):
    try:
        entry = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "type": event_type,
            **data,
        }
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[log] failed: {e}")


# ============================================================
# MEMORY (human-editable notes)
# ============================================================
def load_memory():
    try:
        if MEMORY_PATH.exists():
            return MEMORY_PATH.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[memory] failed: {e}")
    return "(no memory file yet)"


def append_memory(line):
    try:
        header = "\n## Auto-learned lessons\n"
        content = MEMORY_PATH.read_text(encoding="utf-8") if MEMORY_PATH.exists() else ""
        if "## Auto-learned lessons" not in content:
            content += header
        content += f"- [{time.strftime('%Y-%m-%d')}] {line}\n"
        MEMORY_PATH.write_text(content, encoding="utf-8")
        return True
    except Exception as e:
        print(f"[memory] append failed: {e}")
        return False


# ============================================================
# AUTO-LEARNING
# ============================================================
def auto_learn(task, history, outcome, key_actions):
    if not AUTO_LEARN:
        return
    if not history:
        return
    hist_text = "\n".join(history[-6:])
    prompt = (
        f"TASK: {task}\n"
        f"OUTCOME: {outcome}\n"
        f"ACTIONS:\n{hist_text}\n\n"
        "Based on this task, is there ONE short rule worth remembering for future tasks "
        "(about this user, this machine, or this workflow)?\n"
        "Reply with a single sentence rule, no more than 25 words. "
        "Do NOT repeat a rule already implied by the actions above. "
        "If nothing notable, reply exactly: NOTHING"
    )
    try:
        resp = ollama_generate(
            prompt,
            system="You extract one-sentence lessons from task logs.",
            json_mode=False,
        ).strip()

        if not resp or "NOTHING" in resp.upper()[:20]:
            return
        if len(resp) > 250:
            return
        lesson = resp.lstrip("-•* ").strip()
        if append_memory(lesson):
            print(f"[learn] saved: {lesson[:100]}")
            log_event("auto_learn", task=task, lesson=lesson[:200])
    except Exception as e:
        print(f"[learn] failed: {e}")


# ============================================================
# ROUNDTABLE INTEGRATION
# ============================================================
def roundtable_get_latest_conclusion():
    if not ROUNDTABLE_DB.exists():
        return None
    try:
        conn = sqlite3.connect(ROUNDTABLE_DB)
        c = conn.cursor()
        row = c.execute(
            "SELECT session_id, content, created_at FROM messages "
            "WHERE role='conclusion' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            return {
                "session_id": row[0],
                "content": row[1],
                "created_at": row[2],
            }
    except Exception as e:
        print(f"[roundtable] error: {e}")
    return None


def roundtable_as_task():
    concl = roundtable_get_latest_conclusion()
    if not concl:
        return None
    task = (
        "Execute the following plan from my AI roundtable. "
        "Complete the actionable steps using your tools. "
        "Here is the plan:\n\n" + concl["content"]
    )
    return task


# ============================================================
# RAG — embeddings over past tasks
# ============================================================
_rag_cache = None
_rag_available = None


def _rag_model_ready():
    global _rag_available
    if _rag_available is None:
        _rag_available = ollama_has_model(EMBED_MODEL)
    return _rag_available


def _load_rag():
    global _rag_cache
    if _rag_cache is not None:
        return _rag_cache
    try:
        if RAG_PATH.exists():
            _rag_cache = json.loads(RAG_PATH.read_text(encoding="utf-8"))
        else:
            _rag_cache = []
    except Exception:
        _rag_cache = []
    return _rag_cache


def _save_rag():
    try:
        if _rag_cache is not None:
            RAG_PATH.write_text(json.dumps(_rag_cache, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[rag] save failed: {e}")


def embed_text(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": EMBED_MODEL, "input": text},
            timeout=60,
        )
        if r.status_code == 200:
            vecs = r.json().get("embeddings")
            if vecs:
                return vecs[0]
    except Exception:
        pass
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=60,
        )
        if r.status_code == 200:
            return r.json().get("embedding")
    except Exception:
        pass
    return None


def cosine_sim(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def rag_store(task, outcome, steps, key_actions, notes=""):
    global _rag_cache
    if not _rag_model_ready():
        return
    emb = embed_text(task)
    if not emb:
        return
    store = _load_rag()
    store = [e for e in store if e.get("task") != task]
    store.append({
        "task": task,
        "embedding": emb,
        "outcome": outcome,
        "steps": steps,
        "key_actions": key_actions[:10],
        "notes": notes,
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    if len(store) > 200:
        store = store[-200:]
    _rag_cache = store
    _save_rag()


def rag_recall(query, top_k=RAG_TOP_K, min_score=RAG_MIN_SCORE):
    if not _rag_model_ready():
        return []
    emb = embed_text(query)
    if not emb:
        return []
    store = _load_rag()
    if not store:
        return []
    scored = []
    for entry in store:
        s = cosine_sim(emb, entry.get("embedding") or [])
        if s >= min_score:
            scored.append((s, entry))
    scored.sort(key=lambda x: -x[0])
    return [{"score": round(s, 3), **e} for s, e in scored[:top_k]]


def format_rag_context(matches):
    if not matches:
        return "(no similar past tasks)"
    lines = []
    for m in matches:
        note = f" — {m.get('notes','')}" if m.get("notes") else ""
        actions = ", ".join(m.get("key_actions", [])[:3])
        lines.append(
            f"- [{m.get('outcome','?')}] \"{m.get('task','')[:80]}\" "
            f"({m.get('steps','?')} steps){note}"
            + (f"\n    actions: {actions}" if actions else "")
        )
    return "\n".join(lines)


# ============================================================
# MIND
# ============================================================
def ollama_generate(prompt, model=MIND_MODEL, system=None, json_mode=False):
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {"model": model, "messages": messages, "stream": False}
    if json_mode:
        payload["format"] = "json"

    r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=180)
    r.raise_for_status()
    return r.json().get("message", {}).get("content", "").strip()


def ollama_vision(prompt, image_b64, model=VISION_MODEL):
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
    }
    r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=300)
    if r.status_code != 200:
        print(f"[vision-debug] status={r.status_code} body={r.text[:500]}")
    r.raise_for_status()
    return r.json().get("response", "").strip()


def ollama_up():
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def ollama_has_model(name):
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        models = [m["name"] for m in r.json().get("models", [])]
        for m in models:
            if m == name or m.startswith(name + ":") or m.split(":")[0] == name.split(":")[0]:
                return True
        return False
    except Exception:
        return False


def warmup_models():
    if not ollama_has_model(EMBED_MODEL):
        print(f"[warmup] {EMBED_MODEL} not installed — RAG disabled.")
        return
    print(f"[warmup] loading {EMBED_MODEL}...")
    t0 = time.time()
    emb = embed_text("warmup")
    if emb:
        print(f"[warmup] embedding model ready ({len(emb)}-dim, {time.time()-t0:.1f}s)")


# ============================================================
# EYES
# ============================================================
def capture_screen():
    mx, my = pyautogui.position()
    with mss.MSS() as sct:
        target = sct.monitors[1]
        for mon in sct.monitors[1:]:
            if (mon["left"] <= mx < mon["left"] + mon["width"] and
                mon["top"]  <= my < mon["top"]  + mon["height"]):
                target = mon
                break
        shot = sct.grab(target)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    orig_w, orig_h = img.size
    target_w = SCREEN_MAX_W
    target_h = int(orig_h * target_w / orig_w)
    img_small = img.resize((target_w, target_h))

    buf = io.BytesIO()
    img_small.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return b64, orig_w, orig_h, target_w, target_h


def describe_screen(question):
    b64, _, _, _, _ = capture_screen()
    return ollama_vision(f"Question: {question}\nAnswer:", b64)


def read_screen_text():
    b64, _, _, _, _ = capture_screen()
    return ollama_vision(
        "Question: List all visible text on this screen, line by line.\nAnswer:",
        b64,
    )


# ============================================================
# WINDOWS UI AUTOMATION
# ============================================================
def _uia_available():
    try:
        import uiautomation  # noqa
        return True
    except Exception:
        return False


def uia_list_windows():
    if not _uia_available():
        return "FAILED: uiautomation not installed"
    try:
        import uiautomation as auto
        lines = []
        for w in auto.GetRootControl().GetChildren():
            try:
                name = (w.Name or "").strip()
                if not name:
                    continue
                rect = w.BoundingRectangle
                if rect is None:
                    continue
                lines.append(
                    f"- \"{name}\" [{w.ControlTypeName}] "
                    f"@ ({rect.left},{rect.top},{rect.right},{rect.bottom})"
                )
            except Exception:
                continue
        return "OK: windows:\n" + "\n".join(lines[:40]) if lines else "OK: no windows found"
    except Exception as e:
        return f"FAILED: {e}"


def uia_focus_window(title_substr):
    if not _uia_available():
        return "FAILED: uiautomation not installed"
    try:
        import uiautomation as auto
        needle = (title_substr or "").lower()
        for w in auto.GetRootControl().GetChildren():
            try:
                name = (w.Name or "").lower()
                if needle and needle in name:
                    w.SetActive()
                    time.sleep(0.3)
                    return f"OK: focused '{w.Name}'"
            except Exception:
                continue
        return f"FAILED: no window matching '{title_substr}'"
    except Exception as e:
        return f"FAILED: {e}"


def uia_list_controls(window_title, limit=40):
    if not _uia_available():
        return "FAILED: uiautomation not installed"
    try:
        import uiautomation as auto
        needle = (window_title or "").lower()
        target = None
        for w in auto.GetRootControl().GetChildren():
            try:
                if needle in (w.Name or "").lower():
                    target = w
                    break
            except Exception:
                continue
        if not target:
            return f"FAILED: window '{window_title}' not found"
        lines = []
        for ctrl, _depth in auto.WalkControl(target, maxDepth=10):
            try:
                ct = ctrl.ControlTypeName
                if ct not in ("ButtonControl", "EditControl", "ComboBoxControl",
                              "MenuItemControl", "TabItemControl", "ListItemControl",
                              "HyperlinkControl", "CheckBoxControl", "RadioButtonControl",
                              "TextControl"):
                    continue
                name = (ctrl.Name or "").strip()
                rect = ctrl.BoundingRectangle
                if rect is None:
                    continue
                if not name and ct != "EditControl":
                    continue
                lines.append(
                    f"- [{ct.replace('Control','')}] \"{name}\" "
                    f"@ ({rect.left},{rect.top})"
                )
                if len(lines) >= limit:
                    break
            except Exception:
                continue
        if not lines:
            return f"OK: no named controls found in '{window_title}'"
        return f"OK: controls in '{window_title}':\n" + "\n".join(lines)
    except Exception as e:
        return f"FAILED: {e}"


def uia_click_control(window_title, control_name):
    if not _uia_available():
        return "FAILED: uiautomation not installed"
    try:
        import uiautomation as auto
        wn = (window_title or "").lower()
        cn = (control_name or "").lower()
        target_win = None
        for w in auto.GetRootControl().GetChildren():
            try:
                if wn in (w.Name or "").lower():
                    target_win = w
                    break
            except Exception:
                continue
        if not target_win:
            return f"FAILED: window '{window_title}' not found"
        for ctrl, _depth in auto.WalkControl(target_win, maxDepth=10):
            try:
                if (ctrl.Name or "").lower() == cn:
                    ctrl.Click()
                    time.sleep(0.3)
                    return f"OK: clicked '{ctrl.Name}' in '{target_win.Name}'"
            except Exception:
                continue
        return f"FAILED: control '{control_name}' not found in '{window_title}'"
    except Exception as e:
        return f"FAILED: {e}"


def uia_type_into(window_title, control_name, text):
    if not _uia_available():
        return "FAILED: uiautomation not installed"
    try:
        import uiautomation as auto
        wn = (window_title or "").lower()
        cn = (control_name or "").lower()
        target_win = None
        for w in auto.GetRootControl().GetChildren():
            try:
                if wn in (w.Name or "").lower():
                    target_win = w
                    break
            except Exception:
                continue
        if not target_win:
            return f"FAILED: window '{window_title}' not found"
        for ctrl, _depth in auto.WalkControl(target_win, maxDepth=10):
            try:
                if (ctrl.Name or "").lower() == cn and ctrl.ControlTypeName in (
                    "EditControl", "DocumentControl", "ComboBoxControl"
                ):
                    ctrl.SetFocus()
                    time.sleep(0.2)
                    try:
                        ctrl.GetValuePattern().SetValue(text)
                        return f"OK: set '{ctrl.Name}' to '{text[:40]}'"
                    except Exception:
                        pass
                    pyautogui.typewrite(text, interval=0.02)
                    return f"OK: typed into '{ctrl.Name}'"
            except Exception:
                continue
        return f"FAILED: editable control '{control_name}' not found"
    except Exception as e:
        return f"FAILED: {e}"


# ============================================================
# HANDS
# ============================================================
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.4

_last_opened_title = None
_last_opened_hint = None


def _foreground_is_terminal():
    try:
        import pygetwindow as gw
        active = gw.getActiveWindow()
        if not active:
            return False
        t = (active.title or "").lower()
        return any(k in t for k in ["cmd", "powershell", "terminal", "python"])
    except Exception:
        return False


def _refocus_last_opened():
    global _last_opened_title, _last_opened_hint
    if not _last_opened_title and not _last_opened_hint:
        return False
    try:
        import pygetwindow as gw
        if _last_opened_title:
            for w in gw.getAllWindows():
                if w.title == _last_opened_title and w.visible and not w.isMinimized:
                    try:
                        w.activate(); time.sleep(0.25); return True
                    except Exception:
                        pass
        if _last_opened_hint:
            hint = _last_opened_hint.lower()
            for w in gw.getAllWindows():
                if hint in (w.title or "").lower() and w.visible and not w.isMinimized:
                    try:
                        w.activate(); time.sleep(0.25)
                        _last_opened_title = w.title
                        return True
                    except Exception:
                        pass
    except Exception:
        pass
    return False


def action_click(x, y):
    pyautogui.click(x, y)


def action_double_click(x, y):
    pyautogui.doubleClick(x, y)


def action_type(text):
    _refocus_last_opened()
    if _foreground_is_terminal():
        raise RuntimeError("Refusing to type: foreground is a terminal.")
    try:
        import pyperclip
        try:
            old = pyperclip.paste()
        except Exception:
            old = ""
        pyperclip.copy(text)
        time.sleep(0.15)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.15)
        try:
            pyperclip.copy(old)
        except Exception:
            pass
    except ImportError:
        pyautogui.typewrite(text, interval=0.02)


def action_hotkey(keys):
    pyautogui.hotkey(*keys)


def action_move(x, y):
    pyautogui.moveTo(x, y, duration=0.3)


def action_shell(cmd):
    cmd_stripped = cmd.strip()
    cmd_lower = cmd_stripped.lower()
    launches_gui = (
        cmd_lower.startswith("start ") or
        cmd_lower.startswith("explorer ") or
        cmd_lower in ("notepad", "notepad.exe", "calc", "mspaint", "explorer")
    )
    if launches_gui:
        try:
            subprocess.Popen(cmd_stripped, shell=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "OK (launched GUI app, running in background)"
        except Exception as e:
            return f"FAILED: {e}"
    try:
        out = subprocess.check_output(
            cmd, shell=True, stderr=subprocess.STDOUT,
            timeout=SHELL_TIMEOUT, text=True
        )
        out = out.strip()
        return out[:2000] if out else "OK (command ran, no output)"
    except subprocess.CalledProcessError as e:
        err = (e.output or "").strip()
        return f"FAILED (exit {e.returncode}): {err[:1000] or '(no error)'}"
    except subprocess.TimeoutExpired:
        return f"FAILED: timeout after {SHELL_TIMEOUT}s"


def action_cmd_window(command):
    safe_cmd = command.strip()
    try:
        subprocess.Popen(
            f'start cmd /k "{safe_cmd}"',
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return f"OK: opened cmd window running: {safe_cmd[:120]}"
    except Exception as e:
        return f"FAILED: {e}"


def action_open(path):
    global _last_opened_title, _last_opened_hint
    if sys.platform == "win32":
        os.startfile(path)
        try:
            import pygetwindow as gw
            base = Path(path).stem.lower()
            _last_opened_hint = base if base and base != path.lower() else None
            for _ in range(10):
                time.sleep(0.5)
                for w in gw.getAllWindows():
                    title = (w.title or "").lower()
                    if base in title and w.visible:
                        try:
                            w.activate()
                        except Exception:
                            pass
                        _last_opened_title = w.title
                        return
        except Exception:
            pass
        time.sleep(1.0)
    elif sys.platform == "darwin":
        subprocess.run(["open", path]); time.sleep(1.5)
    else:
        subprocess.run(["xdg-open", path]); time.sleep(1.5)


def action_write_file(path, content):
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        rb = p.read_text(encoding="utf-8")
        if rb == content:
            preview = content[:200].replace("\n", "\\n")
            return f"OK: wrote {len(content)} chars to {path} (verified). Content: '{preview}'"
        return f"WARN: wrote {len(content)} chars, readback {len(rb)} chars"
    except Exception as e:
        return f"FAILED: {e}"


def action_read_file(path):
    try:
        p = Path(path)
        if not p.exists():
            return f"FAILED: file not found: {path}"
        c = p.read_text(encoding="utf-8")
        return f"OK: {len(c)} chars from {path}:\n---\n{c[:1500]}\n---"
    except Exception as e:
        return f"FAILED: {e}"


def action_append_file(path, content):
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(content)
        rb = p.read_text(encoding="utf-8")
        return f"OK: appended {len(content)} chars; file now {len(rb)} chars"
    except Exception as e:
        return f"FAILED: {e}"


def action_list_dir(path):
    try:
        p = Path(path)
        if not p.exists():
            return f"FAILED: path not found: {path}"
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        lines = [f"{'[DIR] ' if i.is_dir() else '      '}{i.name}" for i in items[:60]]
        return f"OK: {len(items)} items in {path}:\n" + "\n".join(lines)
    except Exception as e:
        return f"FAILED: {e}"


# ============================================================
# VOICE
# ============================================================
_tts_engine = None
_whisper = None


def init_tts():
    global _tts_engine
    try:
        import pyttsx3
        _tts_engine = pyttsx3.init()
        _tts_engine.setProperty("rate", 180)
        _tts_engine.setProperty("volume", 1.0)
    except Exception as e:
        print(f"[tts] disabled: {e}")


def speak(text):
    if not text:
        return
    if _tts_engine is None:
        print(f"🔊 {text}")
        return
    try:
        _tts_engine.say(text)
        _tts_engine.runAndWait()
    except Exception as e:
        print(f"[tts] error: {e}")


def init_whisper():
    global _whisper
    if _whisper is not None:
        return
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("[ears] faster-whisper not installed. Run: pip install faster-whisper")
        return

    print(f"[ears] loading whisper '{WHISPER_SIZE}'...")
    for kwargs, label in [
        ({"local_files_only": True}, "local cache"),
        ({"local_files_only": False}, "download"),
    ]:
        try:
            _whisper = WhisperModel(
                WHISPER_SIZE, device="cpu", compute_type="int8", **kwargs
            )
            print(f"[ears] ready ({label})")
            return
        except Exception as e:
            print(f"[ears] {label} failed: {str(e)[:150]}")

    print("[ears] FAILED to load whisper.")
    print("[ears] To pre-download, run:")
    print("[ears]   pip install huggingface_hub")
    print("[ears]   huggingface-cli download Systran/faster-whisper-" + WHISPER_SIZE)
    _whisper = None


def listen_once(seconds=6):
    if _whisper is None:
        return ""
    try:
        import sounddevice as sd
        import numpy as np
        print(f"🎤 listening for {seconds}s...")
        sr = 16000
        audio = sd.rec(int(sr * seconds), samplerate=sr, channels=1, dtype="float32")
        sd.wait()
        audio = audio.flatten()
        segments, _ = _whisper.transcribe(audio, language="en")
        return " ".join(s.text for s in segments).strip()
    except Exception as e:
        print(f"[ears] error: {e}")
        return ""


# ============================================================
# WATCHER (background thread)
# ============================================================
_watcher_thread = None
_watcher_stop = threading.Event()


def watcher_loop(interval, prompt):
    print(f"[watcher] started (interval={interval}s). Type 'unwatch' to stop.")
    speak("Watcher started.")
    last_alert = ""
    while not _watcher_stop.is_set():
        try:
            desc = describe_screen(prompt).strip()
            if desc and "NOTHING" not in desc.upper()[:30] and desc != last_alert:
                print(f"\n👁  [watcher] {desc[:200]}")
                speak(desc[:150])
                if AWAY_MODE and notify.enabled():
                    notify.send(f"👁 *Watcher alert:*\n{desc[:400]}")
                last_alert = desc
                log_event("watcher_alert", description=desc[:300])
        except Exception as e:
            print(f"[watcher] error: {e}")
        _watcher_stop.wait(interval)
    print("[watcher] stopped")


def start_watcher():
    global _watcher_thread
    if _watcher_thread and _watcher_thread.is_alive():
        print("[watcher] already running")
        return
    _watcher_stop.clear()
    _watcher_thread = threading.Thread(
        target=watcher_loop,
        args=(WATCH_INTERVAL, WATCH_PROMPT),
        daemon=True,
    )
    _watcher_thread.start()


def stop_watcher():
    if _watcher_thread and _watcher_thread.is_alive():
        _watcher_stop.set()
        _watcher_thread.join(timeout=2)
        print("[watcher] stop signal sent")
    else:
        print("[watcher] not running")


# ============================================================
# ORCHESTRATOR
# ============================================================
TOOLS_SCHEMA = """
TOOLS AVAILABLE (all local):

Visual / screen:
- describe_screen("question")                 — vision model describes the screen
- read_screen_text()                          — vision model reads visible text
- list_windows()                              — list open windows with titles
- list_controls("window_title")               — enumerate buttons/edits in a window
- focus_window("partial_title")               — bring a window to front

Clicking / typing:
- click(x, y) / double_click(x, y) / move(x, y)
- click_control("window_title", "control_name")   — MOST RELIABLE click
- type("text")
- type_into("window_title", "control_name", "text")
- hotkey(["ctrl","c"])

Apps / shell:
- open("path")                                — open file/app with default handler
- shell("command")                            — run command hidden, returns output
- cmd_window("command")                       — open VISIBLE cmd window running command

Files:
- write_file("path", "content")               — write + auto-verify
- read_file("path")
- append_file("path", "content")
- list_dir("path")

Mobile (Telegram):
- ask_mobile("question")                      — send a question to user's phone, wait for reply
- notify_mobile("text")                       — send a one-way notification to phone

Other:
- clarify("question")                         — ask the user in the terminal
- speak("text")                               — say out loud
- done("summary") / fail("reason")
"""

PLANNER_PROMPT = f"""You are a local autonomous agent controlling a Windows 11 computer.

SYSTEM CONTEXT:
- Username: 106627 (Shubham)
- Desktop: C:\\Users\\106627\\Desktop
- Documents: C:\\Users\\106627\\Documents
- Shell: cmd.exe. Running inside a terminal.

{TOOLS_SCHEMA}

Respond with ONLY a JSON object describing the NEXT single action.
Format: {{"tool": "click", "args": {{"x": 500, "y": 300}}, "thought": "..."}}
No prose. No markdown fences.

Key names (strict):
- shell: "command"
- cmd_window: "command"
- open: "path"
- type: "text"
- clarify / ask_mobile: "question"
- notify_mobile: "text"
- write_file / append_file: "path" AND "content"
- read_file / list_dir: "path"
- list_controls / focus_window / click_control: "window" and (for click_control) "control"
- type_into: "window", "control", "text"

DECISION RULES:
1. Prefer deterministic tools over vision when possible.
2. Use vision only when you can't determine something structurally.
3. If a task needs info you don't have, use clarify("your question"). Never guess.
4. If the user asks to run a command AND SEE it, use cmd_window. Else use shell.
5. Path rules: NEVER use /path/to/... NEVER use C:\\Users\\Desktop\\...
   Desktop = C:\\Users\\106627\\Desktop
6. NEVER send an empty args dict to a tool.
7. Use ask_mobile only when user input is truly required and the user might be away.

FAILURE RECOVERY:
- If the last 2 actions failed with the same error, try a DIFFERENT approach.
- Do NOT retry the same tool call twice in a row.

TASK BOUNDARIES:
- Do ONLY what the task asks. No extra work.
- "open X" = one open. Then done.
- "open X and type Y" = two actions: open, type.
- "create a file" = one write_file. Then done.
- After write_file returns "OK: ... (verified)", the task is DONE.

DONE RULES:
- The instant the requested action succeeds, call done("summary").
- Better to finish too early than to do extra unwanted work.
"""


def next_action(task, history, memory, rag_context):
    hist_text = "\n".join(history[-8:]) if history else "(no actions yet)"
    prompt = (
        f"USER MEMORY:\n{memory}\n\n"
        f"SIMILAR PAST TASKS (for reference):\n{rag_context}\n\n"
        f"CURRENT TASK: {task}\n\n"
        f"RECENT ACTIONS:\n{hist_text}\n\n"
        "What is the NEXT single action? Reply with JSON only."
    )
    raw = ollama_generate(prompt, system=PLANNER_PROMPT, json_mode=True)
    try:
        return json.loads(raw)
    except Exception:
        s = raw.find("{"); e = raw.rfind("}")
        if s >= 0 and e > s:
            try:
                return json.loads(raw[s:e + 1])
            except Exception:
                pass
        print(f"[mind-debug] non-JSON reply: {raw[:300]}")
        return {"tool": "fail", "args": {"reason": f"bad model output: {raw[:200]}"}}


DESTRUCTIVE_TOOLS = {"shell", "cmd_window"}


def _is_safe_command(cmd):
    c = (cmd or "").strip().lower()
    return any(c.startswith(p) for p in SAFE_COMMAND_PREFIXES)


def confirm(tool, args):
    """Return True to allow, False to skip.

    In AWAY_MODE, route destructive prompts to Telegram if available.
    In local mode, use the terminal prompt (SAFE_MODE).
    """
    needs_prompt = (SAFE_MODE and tool in DESTRUCTIVE_TOOLS)

    # Non-destructive: always allow
    if not needs_prompt:
        return True

    # Safe commands skip the prompt
    if tool in ("shell", "cmd_window"):
        cmd = args.get("command") or args.get("cmd") or ""
        if _is_safe_command(cmd):
            print(f"   → (safe command, auto-approved)")
            return True

    question = f"Allow this?\n\n```\n{json.dumps(args)[:300]}\n```\nTool: `{tool}`"

    # AWAY_MODE: try Telegram first
    if AWAY_MODE and notify.enabled():
        ans = notify.ask_yes_no(question, timeout_seconds=600)
        if ans is True:
            print("   → approved via mobile ✅")
            return True
        if ans is False:
            print("   → denied via mobile ❌")
            return False
        print("   → no reply from mobile, denying (safety)")
        return False

    # Local prompt
    print(f"\n⚠️  About to run: {tool}({args})")
    ans = input("   Allow? [y/N] ").strip().lower()
    return ans == "y"


REQUIRED_ARGS = {
    "click":         ["x", "y"],
    "double_click":  ["x", "y"],
    "move":          ["x", "y"],
    "shell":         ["command"],
    "cmd_window":    ["command"],
    "open":          ["path"],
    "type":          ["text"],
    "hotkey":        ["keys"],
    "write_file":    ["path", "content"],
    "read_file":     ["path"],
    "append_file":   ["path", "content"],
    "list_dir":      ["path"],
    "focus_window":  ["window"],
    "list_controls": ["window"],
    "click_control": ["window", "control"],
    "type_into":     ["window", "control", "text"],
    "clarify":       ["question"],
    "ask_mobile":    ["question"],
    "notify_mobile": ["text"],
    "speak":         ["text"],
}

ARG_ALIASES = {
    "command":  ["command", "cmd", "shell", "command_line"],
    "path":     ["path", "file", "target"],
    "text":     ["text", "content", "string", "message"],
    "window":   ["window", "window_title", "title"],
    "control":  ["control", "name", "control_name"],
    "question": ["question", "query", "text"],
}


def _validate_args(tool, args):
    required = REQUIRED_ARGS.get(tool)
    if not required:
        return None
    for key in required:
        candidates = ARG_ALIASES.get(key, [key])
        if not any(args.get(c) for c in candidates):
            return (
                f"error: {tool} needs '{key}' "
                f"(accepted keys: {candidates}). Got keys: {list(args.keys())}"
            )
    return None


def execute(tool, args):
    err = _validate_args(tool, args)
    if err:
        return err

    def real_coords(x, y):
        mx, my = pyautogui.position()
        with mss.MSS() as sct:
            for mon in sct.monitors[1:]:
                if (mon["left"] <= mx < mon["left"] + mon["width"] and
                    mon["top"]  <= my < mon["top"]  + mon["height"]):
                    return x + mon["left"], y + mon["top"]
        return x, y

    if tool == "click":
        rx, ry = real_coords(int(args["x"]), int(args["y"]))
        action_click(rx, ry); return f"clicked ({rx},{ry})"
    if tool == "double_click":
        rx, ry = real_coords(int(args["x"]), int(args["y"]))
        action_double_click(rx, ry); return f"double-clicked ({rx},{ry})"
    if tool == "move":
        rx, ry = real_coords(int(args["x"]), int(args["y"]))
        action_move(rx, ry); return f"moved to ({rx},{ry})"
    if tool == "type":
        action_type(args.get("text") or args.get("content") or ""); return "typed"
    if tool == "hotkey":
        keys = args.get("keys", [])
        if isinstance(keys, str): keys = keys.split("+")
        action_hotkey(keys); return f"pressed {'+'.join(keys)}"
    if tool == "open":
        path = args.get("path") or args.get("file")
        action_open(path); return f"opened {path}"
    if tool == "shell":
        cmd = args.get("command") or args.get("cmd")
        return f"shell: {action_shell(cmd)[:200]}"
    if tool == "cmd_window":
        cmd = args.get("command") or args.get("cmd")
        return action_cmd_window(cmd)
    if tool == "write_file":
        p = args.get("path") or args.get("file")
        c = args.get("content") or args.get("text") or ""
        return action_write_file(p, c)
    if tool == "read_file":
        p = args.get("path") or args.get("file")
        return action_read_file(p)
    if tool == "append_file":
        p = args.get("path") or args.get("file")
        c = args.get("content") or args.get("text") or ""
        return action_append_file(p, c)
    if tool == "list_dir":
        return action_list_dir(args.get("path") or ".")
    if tool == "describe_screen":
        q = args.get("question") or "What's on screen?"
        return f"screen: {describe_screen(q)[:500]}"
    if tool == "read_screen_text":
        return f"screen text: {read_screen_text()[:1000]}"
    if tool == "list_windows":
        return uia_list_windows()
    if tool == "list_controls":
        w = args.get("window") or args.get("window_title") or args.get("title")
        return uia_list_controls(w)
    if tool == "focus_window":
        w = args.get("window") or args.get("title")
        return uia_focus_window(w)
    if tool == "click_control":
        w = args.get("window") or args.get("window_title")
        c = args.get("control") or args.get("name")
        return uia_click_control(w, c)
    if tool == "type_into":
        w = args.get("window") or args.get("window_title")
        c = args.get("control") or args.get("name")
        t = args.get("text") or args.get("content") or ""
        return uia_type_into(w, c, t)
    if tool == "clarify":
        q = args.get("question") or args.get("text") or "Please clarify."
        print(f"\n❓ Agent asks: {q}")
        try: a = input("   Your answer: ").strip()
        except EOFError: a = ""
        return f"User answered: {a or '(no answer)'}"
    if tool == "ask_mobile":
        q = args.get("question") or args.get("text") or "Please reply."
        if not notify.enabled():
            return "FAILED: telegram not configured"
        ans = notify.ask(q, timeout_seconds=300)
        return f"User replied on mobile: {ans}" if ans else "User did not reply (timeout)"
    if tool == "notify_mobile":
        text = args.get("text") or args.get("message") or ""
        if not notify.enabled():
            return "FAILED: telegram not configured"
        ok = notify.send(f"📢 {text}")
        return "sent" if ok else "FAILED: send failed"
    if tool == "speak":
        speak(args.get("text") or args.get("message") or ""); return "spoke"
    return f"unknown tool: {tool}"


MULTI_STEP_MARKERS = (" and ", " then ", " also ", " followed by ")
SINGLE_ACTION_VERBS = ("open", "launch", "start", "close", "kill", "exit", "quit")


def _is_simple_task(task):
    t = (task or "").lower().strip()
    if not t: return False
    if any(m in t for m in MULTI_STEP_MARKERS): return False
    words = t.split()
    return bool(words) and words[0] in SINGLE_ACTION_VERBS


def run_task(task, speak_replies=False, quiet=False):
    """Run one full task to completion. Returns outcome string."""
    if not quiet:
        print(f"\n🎯 TASK: {task}")
    if speak_replies:
        speak(f"Starting: {task}")

    log_event("task_start", task=task)
    t0 = time.time()

    memory = load_memory()

    if _rag_model_ready():
        if not quiet:
            print("[rag] searching for similar past tasks...")
        matches = rag_recall(task)
        rag_context = format_rag_context(matches)
        if matches and not quiet:
            print(f"[rag] {len(matches)} similar task(s) found:")
            for m in matches:
                print(f"      {m['score']}  {m['task'][:60]}")
        elif not quiet:
            print("[rag] no close matches")
    else:
        rag_context = "(RAG disabled — embedding model not installed)"

    history = []
    last_sig = None
    repeat = 0
    key_actions = []

    def _finalize(outcome, summary=""):
        rag_store(task, outcome, len(history), key_actions, notes=summary[:80])
        auto_learn(task, history, outcome, key_actions)
        return outcome

    for step in range(1, MAX_STEPS + 1):
        if not quiet:
            print(f"\n--- Step {step}/{MAX_STEPS} ---")
        step_t0 = time.time()
        decision = next_action(task, history, memory, rag_context)

        tool = decision.get("tool", "fail")
        args = decision.get("args", {})
        thought = decision.get("thought", "")
        if thought and not quiet:
            print(f"💭 {thought}")

        if tool == "done":
            msg = args.get("summary", "done")
            elapsed = time.time() - t0
            if not quiet:
                print(f"✅ {msg}  ({elapsed:.1f}s total)")
            log_event("task_done", task=task, summary=msg, steps=step, elapsed=round(elapsed, 2))
            _finalize("success", msg)
            if AWAY_MODE and notify.enabled():
                notify.send(f"✅ Task done:\n*{task[:80]}*\n\n{msg[:200]}")
            if speak_replies: speak(msg)
            return "success"
        if tool == "fail":
            msg = args.get("reason") or decision.get("thought") or "unknown failure"
            elapsed = time.time() - t0
            if not quiet:
                print(f"❌ {msg}  ({elapsed:.1f}s)")
            log_event("task_failed", task=task, reason=msg, steps=step, elapsed=round(elapsed, 2))
            _finalize("fail", msg)
            if AWAY_MODE and notify.enabled():
                notify.send(f"❌ Task failed:\n*{task[:80]}*\n\n{msg[:200]}")
            if speak_replies: speak("I couldn't finish: " + msg)
            return "fail"

        sig_exact = f"{tool}:{json.dumps(args, sort_keys=True)}"
        if sig_exact == last_sig:
            repeat += 1
        else:
            repeat = 1
            last_sig = sig_exact

        recent_tools = [h.split("(", 1)[0] for h in history[-6:]]
        pattern_repeat = recent_tools.count(tool) if recent_tools else 0

        if repeat >= 2 or pattern_repeat >= 4:
            reason = "same action" if repeat >= 2 else f"tool '{tool}' x{pattern_repeat}"
            if not quiet:
                print(f"   → LOOP DETECTED: {reason}.")
            history.append(
                f"LOOP WARNING: You used '{tool}' repeatedly without success. "
                f"Stop. Either call done() if the task is complete, or use a COMPLETELY "
                f"different tool."
            )
            log_event("loop_warning", tool=tool, args=args, step=step,
                      repeat_count=repeat, pattern_repeat=pattern_repeat)
            if repeat >= 3 or pattern_repeat >= 6:
                if not quiet:
                    print(f"❌ Giving up: stuck in a loop.")
                log_event("task_loop_giveup", task=task, steps=step)
                _finalize("loop")
                if AWAY_MODE and notify.enabled():
                    notify.send(f"🔁 Task looped and gave up:\n*{task[:80]}*")
                return "loop"
            continue

        if not confirm(tool, args):
            if not quiet:
                print("   → skipped by user")
            history.append(f"{tool}: SKIPPED by user")
            log_event("action_skipped", tool=tool, args=args, step=step)
            continue

        try:
            result = execute(tool, args)
            dt = time.time() - step_t0
            if not quiet:
                print(f"   → {result}  ({dt:.1f}s)")
            history.append(f"{tool}({args}) → {result[:200]}")
            log_event("action", tool=tool, args=args, result=str(result)[:500],
                      step=step, elapsed=round(dt, 2))
            if tool in ("open", "shell", "cmd_window", "write_file", "click_control",
                        "type_into", "focus_window"):
                key_actions.append(f"{tool}({json.dumps(args)[:60]})")

            if (step == 1 and _is_simple_task(task) and tool in {"open", "shell", "hotkey"}
                and "error" not in result.lower() and "failed" not in result.lower()):
                if not quiet:
                    print(f"✅ Task appears complete after 1 action. Stopping.")
                log_event("task_done", task=task, summary=result, steps=1)
                _finalize("success", result)
                if AWAY_MODE and notify.enabled():
                    notify.send(f"✅ Task done:\n*{task[:80]}*\n\n{result[:200]}")
                return "success"
        except Exception as e:
            dt = time.time() - step_t0
            if not quiet:
                print(f"   → error: {e}  ({dt:.1f}s)")
            history.append(f"{tool}({args}) → ERROR: {e}")
            log_event("action_error", tool=tool, args=args, error=str(e), step=step)

        time.sleep(STEP_DELAY)

    if not quiet:
        print(f"\n⚠️  Hit max steps ({MAX_STEPS}).")
    log_event("task_maxsteps", task=task, steps=MAX_STEPS)
    _finalize("maxsteps")
    if AWAY_MODE and notify.enabled():
        notify.send(f"⚠️ Task hit max steps:\n*{task[:80]}*")
    if speak_replies: speak("I ran out of steps.")
    return "maxsteps"


# ============================================================
# MULTI-TASK QUEUE
# ============================================================
def run_queue(tasks, speak_replies=False):
    if not tasks:
        print("(empty queue)")
        return
    print(f"\n📋 QUEUE: {len(tasks)} task(s)")
    log_event("queue_start", count=len(tasks), tasks=tasks)
    results = []
    for i, t in enumerate(tasks, 1):
        print(f"\n{'=' * 60}\n[{i}/{len(tasks)}] {t}\n{'=' * 60}")
        try:
            outcome = run_task(t, speak_replies=speak_replies, quiet=False)
            results.append({"task": t, "outcome": outcome})
        except KeyboardInterrupt:
            print("\n⚠️  Queue interrupted by user.")
            results.append({"task": t, "outcome": "interrupted"})
            break
        except Exception as e:
            print(f"⚠️  Task crashed: {e}")
            results.append({"task": t, "outcome": f"crash: {e}"})

    print(f"\n{'=' * 60}\n📋 QUEUE COMPLETE\n{'=' * 60}")
    for r in results:
        icon = {"success": "✅", "fail": "❌", "loop": "🔁",
                "maxsteps": "⚠️", "interrupted": "🛑"}.get(r["outcome"], "•")
        print(f"  {icon} [{r['outcome']}] {r['task'][:70]}")
    log_event("queue_done", results=results)

    if AWAY_MODE and notify.enabled():
        lines = [f"📋 Queue complete ({len(results)} tasks):"]
        for r in results:
            icon = {"success": "✅", "fail": "❌", "loop": "🔁",
                    "maxsteps": "⚠️", "interrupted": "🛑"}.get(r["outcome"], "•")
            lines.append(f"{icon} {r['task'][:60]}")
        notify.send("\n".join(lines))


def load_queue_from_file(path):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    except Exception as e:
        print(f"[queue] failed to read {path}: {e}")
        return []


# ============================================================
# ENTRY POINTS
# ============================================================
def repl():
    print("🧠 Local Mind (text mode)")
    print("Type 'help' for commands.")
    log_event("session_start")
    while True:
        try:
            cmd = input("\n> ").strip().lstrip(">").strip()
        except (EOFError, KeyboardInterrupt):
            stop_watcher()
            print("\nbye"); log_event("session_end"); return
        if not cmd:
            continue
        low = cmd.lower()

        if low in ("exit", "quit"):
            stop_watcher()
            print("bye"); log_event("session_end"); return

        # ---- Direct tools ----
        if low in ("list_windows", "windows"):
            print(uia_list_windows()); continue
        if low in ("read_screen_text", "text"):
            print(read_screen_text()); continue
        if low in ("describe_screen", "screen"):
            print(describe_screen("Describe what's on this screen.")); continue
        if low in ("list_controls", "controls"):
            w = input("   window title substring: ").strip()
            if w: print(uia_list_controls(w))
            continue
        if low == "focus":
            w = input("   window title substring: ").strip()
            if w: print(uia_focus_window(w))
            continue

        # ---- RAG ----
        if low == "rag":
            store = _load_rag()
            print(f"\n--- RAG memory: {len(store)} tasks ---")
            for e in store[-15:]:
                print(f"  [{e.get('outcome','?')}] {e.get('task','')[:70]}")
            continue
        if low == "rag_clear":
            global _rag_cache
            _rag_cache = []
            _save_rag()
            print("RAG cleared"); continue

        # ---- Queue ----
        if low == "queue":
            print("Enter tasks one per line. Blank line finishes. ('#' comments OK)")
            tasks = []
            while True:
                try:
                    line = input("  | ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not line:
                    break
                if line.startswith("#"):
                    continue
                tasks.append(line)
            run_queue(tasks)
            continue
        if low.startswith("run "):
            path = cmd[4:].strip()
            tasks = load_queue_from_file(path)
            if tasks:
                run_queue(tasks)
            continue

        # ---- Roundtable ----
        if low == "roundtable":
            task = roundtable_as_task()
            if not task:
                print("[roundtable] no conclusion found in DB.")
                print(f"   DB path: {ROUNDTABLE_DB}")
                continue
            concl = roundtable_get_latest_conclusion()
            print(f"[roundtable] latest conclusion from session #{concl['session_id']} ({concl['created_at']})")
            print("─" * 60)
            print(concl["content"][:600])
            print("─" * 60)
            ans = input("Run this as a task? [y/N] ").strip().lower()
            if ans == "y":
                run_task(task)
            continue

        # ---- Watcher ----
        if low == "watch":
            start_watcher(); continue
        if low == "unwatch":
            stop_watcher(); continue

        # ---- Auto-learn toggle ----
        if low == "learn":
            print(f"[learn] AUTO_LEARN is currently: {'ON' if AUTO_LEARN else 'OFF'}")
            continue
        if low == "learn on":
            globals()["AUTO_LEARN"] = True
            print("[learn] ON"); continue
        if low == "learn off":
            globals()["AUTO_LEARN"] = False
            print("[learn] OFF"); continue

        # ---- Mobile / Telegram ----
        if low == "away":
            globals()["AWAY_MODE"] = True
            print("[away] ON — confirmations and reports will go to Telegram")
            if notify.enabled():
                notify.send("🟡 Away mode is ON. Confirmations will arrive here.")
            continue
        if low == "back":
            globals()["AWAY_MODE"] = False
            print("[away] OFF — back to local terminal prompts")
            if notify.enabled():
                notify.send("🟢 Away mode is OFF.")
            continue
        if low == "notify status":
            if notify.enabled():
                print(f"[notify] enabled (chat_id={notify.CHAT_ID[:4]}...)")
            else:
                print("[notify] NOT configured. Add TELEGRAM_TOKEN and TELEGRAM_CHAT_ID to .env")
            continue
        if low == "notify test":
            if not notify.enabled():
                print("[notify] not configured.")
                continue
            if notify.send("🔔 Test from Local Mind"):
                print("[notify] test sent. Check your phone.")
            else:
                print("[notify] send failed.")
            continue
        if low.startswith("notify ask "):
            q = cmd[11:].strip()
            if not notify.enabled():
                print("[notify] not configured.")
                continue
            ans = notify.ask(q, timeout_seconds=300)
            print(f"[notify] user replied: {ans!r}")
            continue

        # ---- Whisper download helper ----
        if low in ("whisper-download", "download-whisper"):
            print(f"[whisper] downloading Systran/faster-whisper-{WHISPER_SIZE}...")
            try:
                from huggingface_hub import snapshot_download
                snapshot_download(f"Systran/faster-whisper-{WHISPER_SIZE}")
                print("[whisper] downloaded. Voice mode should now work.")
            except Exception as e:
                print(f"[whisper] failed: {e}")
            continue

        # ---- Logs / info ----
        if low == "tasks":
            print("\n--- last 10 tasks ---")
            try:
                lines = LOG_PATH.read_text(encoding="utf-8").strip().splitlines()
                evts = [l for l in lines if any(t in l for t in
                        ('"type": "task_start"', '"type": "task_done"',
                         '"type": "task_failed"', '"type": "task_loop_giveup"'))]
                for line in evts[-10:]:
                    try:
                        d = json.loads(line); t = d.get("type"); ts = d.get("iso", "")
                        if t == "task_start":
                            print(f"{ts}  START  {d.get('task','')[:70]}")
                        elif t == "task_done":
                            print(f"{ts}  DONE   steps={d.get('steps')}  {d.get('summary','')[:60]}")
                        elif t == "task_failed":
                            print(f"{ts}  FAIL   {d.get('reason','')[:70]}")
                        elif t == "task_loop_giveup":
                            print(f"{ts}  LOOP   steps={d.get('steps')}")
                    except Exception:
                        print(line)
            except Exception as e:
                print(f"(no log: {e})")
            continue
        if low == "log":
            print("\n--- last 10 log entries ---")
            try:
                lines = LOG_PATH.read_text(encoding="utf-8").strip().splitlines()
                for line in lines[-10:]:
                    print(line)
            except Exception as e:
                print(f"(no log: {e})")
            continue
        if low == "memory":
            print("\n--- memory.md ---"); print(load_memory()); continue
        if low == "voice":
            print("Switching to voice mode...")
            voice_loop(); continue
        if low == "clear":
            os.system("cls" if os.name == "nt" else "clear"); continue

        if low == "help":
            print("""
DIRECT TOOLS (instant, no LLM):
  windows           — list open windows
  controls          — enumerate controls in a window
  focus             — bring a window to front
  screen            — describe current screen (vision)
  text              — read visible screen text (vision)

MEMORY / LEARNING:
  memory            — show memory.md
  rag               — show stored RAG tasks
  rag_clear         — wipe RAG memory
  learn on|off      — toggle auto-learning
  tasks             — last 10 task events
  log               — last 10 raw log entries

MULTI-TASK:
  queue             — enter multiple tasks (one per line)
  run <file.txt>    — run tasks from a file

ROUNDTABLE INTEGRATION:
  roundtable        — fetch latest conclusion from roundtable DB and run it

BACKGROUND WATCHER:
  watch             — start background screen monitor
  unwatch           — stop watcher

MOBILE (Telegram):
  away              — enable away mode (confirmations → Telegram)
  back              — disable away mode
  notify status     — check if Telegram is configured
  notify test       — send a test Telegram message
  notify ask <text> — send a question to Telegram and wait for reply

VOICE:
  voice             — switch to push-to-talk mode
  whisper-download  — pre-download the Whisper STT model

MISC:
  clear             — clear terminal
  exit              — quit

ANY OTHER TEXT IS TREATED AS A TASK FOR THE AGENT.
""")
            continue

        run_task(cmd)


def voice_loop():
    init_tts()
    init_whisper()
    if _tts_engine:
        speak("Local mind online. Press enter to speak.")
    print("\n🎤 Voice mode. Press Enter to speak. Type 'exit' to quit.")
    while True:
        try:
            cmd = input("\n[Enter to speak] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("bye"); return
        if cmd.lower() in ("exit", "quit"):
            speak("goodbye"); return
        text = listen_once(seconds=6)
        if not text:
            print("(nothing heard, try again)")
            continue
        print(f"👤 you said: {text}")
        run_task(text, speak_replies=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", action="store_true", help="voice mode")
    parser.add_argument("--task", type=str, help="run one task and exit")
    parser.add_argument("--queue", type=str, help="run tasks from a file")
    parser.add_argument("--watch", action="store_true", help="start in watch mode")
    args = parser.parse_args()

    if not ollama_up():
        print("❌ Ollama isn't running. Start it with: ollama serve")
        return

    print(f"🧠 Mind: {MIND_MODEL}")
    print(f"👁  Eyes: {VISION_MODEL}")
    rag_ok = ollama_has_model(EMBED_MODEL)
    print(f"📚 RAG: {EMBED_MODEL}  (installed: {rag_ok})")
    print(f"🛡  SAFE_MODE: {'on' if SAFE_MODE else 'off'}")
    print(f"🖥  UIA: {'available' if _uia_available() else 'NOT INSTALLED'}")
    print(f"🎓 AUTO_LEARN: {'on' if AUTO_LEARN else 'off'}")
    print(f"📱 Telegram: {'ready' if notify.enabled() else 'not configured (see notify.py)'}")

    warmup_models()

    if args.task:
        run_task(args.task)
    elif args.queue:
        tasks = load_queue_from_file(args.queue)
        run_queue(tasks)
    elif args.watch:
        start_watcher()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop_watcher()
    elif args.voice:
        voice_loop()
    else:
        repl()


if __name__ == "__main__":
    main()