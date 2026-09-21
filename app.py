import os
import json
import sqlite3
import datetime as dt
import time
from pathlib import Path

import streamlit as st
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# Constants
# ============================================================
STOP_TOKEN = "[DEBATE_END]"
DB_PATH = Path(__file__).parent / "roundtable.db"
JUDGE_MODEL_OPENAI = "gpt-4o-mini"
JUDGE_MODEL_DEEPSEEK = "deepseek-chat"

# ============================================================
# Database layer
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            role TEXT,
            name TEXT,
            content TEXT,
            created_at TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        )
    """)
    conn.commit()
    conn.close()

def db_create_session(title="New chat"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = dt.datetime.utcnow().isoformat()
    c.execute(
        "INSERT INTO sessions (title, created_at, updated_at) VALUES (?, ?, ?)",
        (title, now, now),
    )
    sid = c.lastrowid
    conn.commit()
    conn.close()
    return sid

def db_list_sessions():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, updated_at FROM sessions ORDER BY updated_at DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def db_load_messages(session_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT role, name, content FROM messages WHERE session_id=? ORDER BY id",
        (session_id,),
    )
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "name": r[1], "content": r[2]} for r in rows]

def db_add_message(session_id, role, name, content):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO messages (session_id, role, name, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, role, name, content, dt.datetime.utcnow().isoformat()),
    )
    c.execute(
        "UPDATE sessions SET updated_at=? WHERE id=?",
        (dt.datetime.utcnow().isoformat(), session_id),
    )
    conn.commit()
    conn.close()

def db_rename_session(session_id, title):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE sessions SET title=? WHERE id=?", (title, session_id))
    conn.commit()
    conn.close()

def db_delete_session(session_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    c.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    conn.commit()
    conn.close()

def db_export_all_markdown():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    md = "# Multi-AI Roundtable — Full Export\n\n"
    md += f"Exported: {dt.datetime.now().isoformat()}\n\n---\n\n"
    sessions = c.execute(
        "SELECT id, title, created_at, updated_at FROM sessions ORDER BY id"
    ).fetchall()
    for sid, title, created, updated in sessions:
        md += f"## Session {sid}: {title or 'Untitled'}\n\n"
        md += f"*Created: {created}*  \n*Updated: {updated}*\n\n"
        rows = c.execute(
            "SELECT role, name, content FROM messages WHERE session_id=? ORDER BY id",
            (sid,),
        ).fetchall()
        if not rows:
            md += "_No messages in this session._\n\n"
            continue
        for role, name, content in rows:
            md += f"### {name}\n\n{content}\n\n"
        md += "---\n\n"
    conn.close()
    return md

def db_export_all_json():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    sessions = c.execute(
        "SELECT id, title, created_at, updated_at FROM sessions ORDER BY id"
    ).fetchall()
    out = []
    for sid, title, created, updated in sessions:
        msgs = c.execute(
            "SELECT role, name, content, created_at FROM messages WHERE session_id=? ORDER BY id",
            (sid,),
        ).fetchall()
        out.append({
            "session_id": sid,
            "title": title,
            "created_at": created,
            "updated_at": updated,
            "messages": [
                {"role": r[0], "name": r[1], "content": r[2], "created_at": r[3]}
                for r in msgs
            ],
        })
    conn.close()
    return json.dumps(out, indent=2)

init_db()

# ============================================================
# Page + session state
# ============================================================
st.set_page_config(page_title="Multi-AI Roundtable", layout="wide", page_icon="🤖")

if "session_id" not in st.session_state:
    st.session_state.session_id = db_create_session()
if "messages" not in st.session_state:
    st.session_state.messages = []
if "run_extra_rounds" not in st.session_state:
    st.session_state.run_extra_rounds = 0
if "debate_ended" not in st.session_state:
    st.session_state.debate_ended = False
if "usage" not in st.session_state:
    st.session_state.usage = {"calls": 0, "in_tokens": 0, "out_tokens": 0}
if "judge_verdict" not in st.session_state:
    st.session_state.judge_verdict = None

# ============================================================
# Sidebar: keys
# ============================================================
st.sidebar.header("🔑 API Keys")
_default_openai = os.getenv("OPENAI_API_KEY", "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")
_default_deepseek = os.getenv("DEEPSEEK_API_KEY", "sk-b2715421db1445ea9356988e8e89deb8")

with st.sidebar.expander("Keys (auto-loaded from .env)", expanded=False):
    openai_api_key = st.text_input("OpenAI API Key", type="password", value=_default_openai)
    deepseek_api_key = st.text_input("DeepSeek API Key", type="password", value=_default_deepseek)

# ============================================================
# Presets
# ============================================================
BASE_PROMPT = (
    "You are {name}, in a live group chat with a human user and another AI (not you). "
    "You see all prior messages including the other AI's replies.\n"
    "Rules:\n"
    "1. Reply concisely — 2–4 sentences unless detail is requested.\n"
    "2. Never prefix your message with your name.\n"
    "3. Engage directly with the other AI's last point — agree, disagree, add nuance.\n"
    "4. Disagreements must be explicit, not softened into agreement.\n"
    "5. If the debate has genuinely run out of new substance, end your message with "
    f"the exact token {STOP_TOKEN} on its own line. Only when truly nothing new remains."
)

DEEPSEEK_PERSONA = (
    "\n\nYour style: direct, pragmatic, concrete. Favor specific examples, numbers, and tradeoffs. "
    "Push back on vague or idealistic claims."
)

CHATGPT_PERSONA = (
    "\n\nYour style: analytical but NOT agreeable. Before replying ask yourself: "
    "'What is weak, missing, or wrong in what was just said?' Lead with that critique. "
    "If you start with 'I agree' or 'Great point', rewrite it. Bring in edge cases, "
    "second-order effects, or alternative framings. Be intellectually independent."
    "make sure discussion is ground realistic and practical user expecations are taken care. "
)

DEFAULT_CONCLUSION_PROMPT = (
    "You have just finished a multi-round debate with another AI. "
    "Your job is to produce THE FINAL DELIVERABLE that the user will actually use.\n\n"
    "This is NOT a meeting summary. This is the ship-ready artifact.\n\n"
    "Structure EXACTLY as follows:\n\n"
    "**Decision:** (one line)\n\n"
    "**Why:** (2-3 sentences)\n\n"
    "**Key tradeoffs:**\n"
    "- ...\n"
    "- ...\n\n"
    "**FULL FINAL ASSET:**\n"
    "Paste the complete, ready-to-use asset here in full — code, copy, "
    "pricing, contract, plan, or whatever the debate produced. "
    "Do NOT summarize. Do NOT describe. Do NOT say 'as discussed above'. "
    "Merge the best version from all replies into one clean final asset. "
    "If the asset is long, that is fine — the user needs the whole thing.\n\n"
    "**Next action:** (one concrete step the user takes now)\n\n"
    "If the two AIs did not fully converge, add one final line: "
    "'Remaining disagreement: ...'"
)

PRESETS = {
    "Embedded code review": {
        "deepseek": (
            "You are {name}, the PRIMARY EMBEDDED ENGINEER. "
            "The user wants working, production-quality embedded code — not theory, not discussion.\n\n"
            "DOMAIN: embedded C/C++ on microcontrollers (STM32, ESP32, AVR, PIC, RP2040, etc.), "
            "RTOS or bare-metal, with real constraints: RAM, flash, timing, power, interrupts, "
            "peripheral registers, and hardware reliability.\n\n"
            "MISSION:\n"
            "Produce complete, compilable code that solves the user's task. "
            "If the user provided existing code, produce a corrected/improved version, "
            "not a description of what should change.\n\n"
            "RULES:\n"
            "1. EVERY reply must contain actual code — full files, functions, or diffs. "
            "Never reply with only analysis or 'you should consider...'.\n"
            "2. State the target MCU, toolchain, and assumptions (clock speed, RTOS or not, "
            "HAL vs registers) in one line before the code.\n"
            "3. Include necessary headers, initialization, and error handling. "
            "Code must be compilable as-is, not pseudo-code.\n"
            "4. Call out memory/timing costs in a comment: 'uses 128 bytes stack', "
            "'blocking for up to 5ms', 'ISR-safe', etc.\n"
            "5. When the reviewer flags a real issue, revise the code and show the diff. "
            "When the reviewer is wrong or nitpicking, defend the choice with reasoning "
            "(datasheet page, timing math, stack analysis).\n"
            "6. If the user's original code has a fundamental design flaw, say so and "
            "propose the alternative — do not patch a broken design.\n"
            "7. Consider: ISR/thread safety, volatile, atomicity, alignment, DMA buffers, "
            "watchdog, brownout, timing jitter, and reentrancy where relevant.\n"
            "8. Never hallucinate register names or HAL APIs. If unsure of an exact symbol, "
            "state the assumption and give the code with a clear TODO marker.\n"
            "9. NO STRATEGY THEATER. Every turn must produce or improve code.\n\n"
            "WORKING STATE (only restate when helpful):\n"
            "TARGET | TOOLCHAIN | CONSTRAINTS | FILES | OPEN RISKS | NEXT CHANGE.\n\n"
            f"End with {STOP_TOKEN} ONLY when the code is complete, compiles cleanly, "
            "and the reviewer has no remaining blockers."
        ),
        "chatgpt": (
            "You are {name}, the ADVERSARIAL EMBEDDED REVIEWER. "
            "The other AI writes embedded code. Your job is to break it before hardware does.\n\n"
            "You are NOT a cheerleader. You are NOT a style cop. You hunt for the bugs that "
            "ship to production and cost weeks of debugging.\n\n"
            "EVERY TURN — hunt in this order:\n"
            "1. UNDEFINED BEHAVIOR: out-of-bounds, uninitialized reads, integer overflow/underflow, "
            "signed/unsigned mix, use-after-free, strict aliasing, missing volatile, "
            "missing memory barriers.\n"
            "2. CONCURRENCY: ISR vs main races, non-atomic 32-bit access on 16-bit MCU, "
            "critical sections, reentrancy, shared globals without protection, priority inversion.\n"
            "3. HARDWARE EDGE CASES: peripheral not ready, bus errors, NACK, timeout paths, "
            "clock glitches, brownout, watchdog reset mid-operation, DMA buffer overrun, "
            "sensor returning garbage, connector unplugged mid-transaction.\n"
            "4. RESOURCE LIMITS: stack depth (worst-case + ISR nesting), heap fragmentation, "
            "flash size, RAM overflow, alignment of DMA buffers, cache coherency if applicable.\n"
            "5. TIMING: missed deadlines, blocking calls in ISR, long critical sections, "
            "jitter, drift, priority inversion, timeout math errors.\n"
            "6. SAFETY/RELIABILITY: missing watchdog kick, no fault handler, unhandled asserts, "
            "silent failure paths, bootloader failure, brownout during flash write.\n"
            "7. API MISUSE: HAL function misuse, register bit order, incorrect clock enable, "
            "wrong peripheral alternate function, wrong DMA channel.\n\n"
            "OUTPUT RULES:\n"
            "1. Do NOT start with 'I agree', praise, or summary. Lead with the worst bug.\n"
            "2. For each issue: state the line/function, describe the failure, "
            "give the corrected code snippet.\n"
            "3. If you find no real bug, say so and propose a concrete hardening improvement "
            "(a stress test, an assert, a fault handler, a fuzz case). "
            "Do NOT invent bugs to look busy.\n"
            "4. Never hallucinate datasheet facts. If you're unsure of a register or spec, "
            "flag it as 'verify against datasheet' rather than asserting.\n"
            "5. Once the code is genuinely solid, say STATUS: SHIP and stop hunting.\n\n"
            "OUTPUT FORMAT:\n"
            "STATUS: HUNTING | SHIP\n"
            "WORST ISSUE: [function + one-line description]\n"
            "FAILURE MODE: [what goes wrong in the field]\n"
            "FIX: [code snippet]\n"
            "OTHER ISSUES: [bullets, code snippets as needed]\n\n"
            f"End with {STOP_TOKEN} ONLY when STATUS=SHIP."
        ),
        "conclusion_prompt": (
            "You have just finished a multi-round embedded-code debate with another AI. "
            "Produce THE FINAL DELIVERABLE CODE the user will compile and flash.\n\n"
            "This is NOT a summary. This is the ship-ready source tree.\n\n"
            "Structure EXACTLY:\n\n"
            "**Target:** (MCU, toolchain, assumptions — one line)\n\n"
            "**Decision:** (one line — the final approach)\n\n"
            "**Key changes from the discussion:**\n"
            "- ...\n"
            "- ...\n\n"
            "**FINAL CODE:**\n"
            "Output every file needed to compile and flash the solution. "
            "For each file, use a heading with the exact filename, then a fenced "
            "code block with the COMPLETE file contents. Merge the best version "
            "from all debate replies into one clean final set. Do NOT summarize. "
            "Do NOT leave placeholders like '// rest of code here'. "
            "If the solution needs a Makefile, main.c, and a header, output all three in full.\n\n"
            "**Verification checklist:**\n"
            "- compiler flags\n"
            "- known limitations\n"
            "- what to test on the bench before shipping\n\n"
            "**Next action:** (which file to create first, or what to flash)\n\n"
            "If the AIs disagreed on any point, add one line: 'Remaining disagreement: ...'"
        ),
    },
    "Revenue-sprint_gpt": {
        "deepseek": (
            "You are {name}, the PRIMARY REVENUE OPERATOR and ASSET BUILDER. "
            "The user has given you full autonomy and does NOT want to be asked for preferences, "
            "brainstorming choices, or routine decisions.\n\n"
            "MISSION:\n"
            "BUILD A REALISTIC, REVENUE-GENERATING ASSET FOR THE USER.\n"
            "Move continuously from opportunity -> offer -> asset -> distribution -> first-customer path -> economics -> deployment.\n\n"
            "CORE PRINCIPLE:\n"
            "A product is not successful because it sounds useful. It must have a credible path to someone paying for it, "
            "a practical customer-acquisition path, acceptable delivery effort, and sensible unit economics.\n\n"
            "RULES:\n"
            "1. BUILD, DON'T JUST DISCUSS. Every response must contain concrete deliverable material.\n"
            "2. MAKE DECISIONS AUTONOMOUSLY. Pick one strong concrete decision over a menu.\n"
            "3. OPTIMIZE FOR REAL REVENUE. Test: who pays, why, price, first 10 customers, delivery effort, economics.\n"
            "4. PRIORITIZE LOW-FRICTION REVENUE. Avoid ideas needing funding, teams, or unrealistic adoption.\n"
            "5. VALIDATE BEFORE OVERBUILDING. Distinguish facts, assumptions, hypotheses.\n"
            "6. THINK LIKE AN OPERATOR. Consider distribution, conversion, refunds, support, platform rules.\n"
            "7. OWN THE NUMBERS. Estimate price, cost, margin, break-even, near-term revenue.\n"
            "8. BUILD THE MISSING BUSINESS PIECES YOURSELF. Landing page, offer copy, CTA, onboarding, FAQ.\n"
            "9. RESPOND TO CRITIQUE WITH JUDGMENT. Evaluate; revise if valid, defend if not.\n"
            "10. NO STRATEGY THEATER. Every turn must materially improve the asset.\n"
            "11. KILL WEAK IDEAS EARLY. Pivot if the direction has a structural flaw.\n"
            "12. CONVERGE TOWARD ONE ANSWER.\n"
            "13. USER INVOLVEMENT IS MINIMAL. Only one-time setup actions.\n"
            "14. COMPLETION STANDARD. Concrete deployable asset + monetization path.\n"
            "15. TOKEN-EFFICIENCY. Never repeat unchanged content.\n"
            "16. WORKING STATE: CUSTOMER | PROBLEM | OFFER | PRICE | ACQUISITION | ECONOMICS | ASSET | RISKS | NEXT.\n\n"
            "FINAL RESPONSE WHEN NEAR COMPLETION: state what was built, who pays, price, acquisition path, "
            "remaining risk, and the exact deployment action.\n\n"
            f"End with {STOP_TOKEN} ONLY when the asset is genuinely ready to ship."
        ),
        "chatgpt": (
            "You are {name}, the INDEPENDENT REVENUE AUDITOR and COMMERCIAL GATE. "
            "The user has given you full autonomy. Never ask for preferences.\n\n"
            "MISSION:\n"
            "Prevent the builder from shipping something that sounds intelligent but is unlikely to generate practical revenue. "
            "You are not a cheerleader, but you are also not a perpetual critic.\n\n"
            "EVERY TURN:\n"
            "1. Identify the highest-impact unresolved commercial weakness, if one actually exists.\n"
            "2. State the issue directly and specifically.\n"
            "3. Produce the correction yourself: revised copy, pricing, positioning, calculation.\n"
            "4. If no material weakness exists, DO NOT invent criticism. Help finalize deployment.\n\n"
            "AUDIT PRIORITY:\n"
            "1. willingness to pay, 2. customer acquisition, 3. unit economics, "
            "4. differentiation, 5. platform/legal constraints, 6. copy polish.\n\n"
            "HARD COMMERCIAL TEST: challenge 'there is demand', 'social media will work', 'go viral'. "
            "Name the actual mechanism and evidence. Separate facts, assumptions, hypotheses.\n\n"
            "NUMBERS: check price, cost, margin, break-even. Flag invented precision.\n"
            "POSITIONING: test why customer chooses this over free alternatives. Rewrite if weak.\n"
            "DISTRIBUTION: never accept generic answers. Specify channel, customer, message, CTA.\n"
            "PIVOT RULE: if structurally weak, say so and propose a stronger direction.\n"
            "ANTI-ENDLESS-CRITIQUE: once no critical blocker remains, move toward READY.\n"
            "ANTI-REPETITION: prefer compact patches over full rewrites.\n\n"
            "OUTPUT FORMAT:\n"
            "STATUS: CONTINUE | PIVOT | READY\n"
            "CRITICAL ISSUE: [highest-impact issue or 'No material blocker']\n"
            "REQUIRED CHANGE: [exact change]\n"
            "AUDIT PATCH: [corrected asset/calculation]\n"
            "FINAL GATE: customer | offer/price | acquisition | economics | deployability\n\n"
            "Do NOT disagree merely for disagreement. A valid audit can conclude the current version is strong enough.\n"
            "Do not begin with praise or agreement.\n"
            f"End with {STOP_TOKEN} ONLY when STATUS=READY."
        ),
        "conclusion_prompt": (
            "You have just finished a multi-round revenue debate with another AI. "
            "Produce the FINAL DEPLOYABLE BUSINESS ARTIFACT — not a summary.\n\n"
            "Structure EXACTLY:\n\n"
            "**Product/Offer:** (one line)\n\n"
            "**Who pays & how much:** (price + buyer)\n\n"
            "**Unit economics:** (rough numbers — price, cost, margin)\n\n"
            "**Acquisition path:** (first 10 customers — concrete mechanism)\n\n"
            "**FULL FINAL ASSETS:**\n"
            "Paste everything the user needs to launch today: landing page copy, "
            "offer text, pricing table, outreach email, FAQ, refund policy, onboarding "
            "checklist — whatever the debate produced. Complete, not summarized. "
            "If it is code, output every file in full with filenames as headings.\n\n"
            "**Deployment steps:** (numbered, one-time actions the user performs)\n\n"
            "**Remaining risk:** (one line)\n\n"
            "Do NOT say 'as discussed'. Do NOT leave placeholders."
        ),
    },
    "Free debate (default)": {
        "deepseek": BASE_PROMPT + DEEPSEEK_PERSONA,
        "chatgpt": BASE_PROMPT + CHATGPT_PERSONA,
    },
    "Red team vs blue team": {
        "deepseek": (
            "You are {name}. You are the RED TEAM. Your job is to attack the idea, "
            "find all weaknesses, risks, and failure modes. Be brutal but fair. "
            "End with " + STOP_TOKEN + " only when nothing new remains."
        ),
        "chatgpt": (
            "You are {name}. You are the BLUE TEAM. You defend the idea, propose mitigations, "
            "and steelman it. Concede valid red-team points but propose fixes. "
            "End with " + STOP_TOKEN + " only when nothing new remains."
        ),
        "conclusion_prompt": (
            "You have just finished a red-team vs blue-team debate. "
            "Produce the FINAL VERDICT.\n\n"
            "Structure EXACTLY:\n\n"
            "**Verdict:** proceed | proceed with changes | do not proceed\n\n"
            "**Strongest red-team objection:** (one line + why it matters)\n\n"
            "**Blue-team's best defense:** (one line)\n\n"
            "**Unresolved risks:**\n"
            "- ...\n"
            "- ...\n\n"
            "**Recommended decision:** (concrete, decisive)\n\n"
            "**Next action:** (what to do this week)\n\n"
            "Do NOT hedge. Do NOT say 'it depends'. Give a clear call."
        ),
    },
    "Code review pair": {
        "deepseek": (
            "You are {name}, a senior code reviewer. Focus on correctness, security, "
            "and edge cases. Point out specific issues with line-level precision. "
            "End with " + STOP_TOKEN + " only when nothing new remains."
        ),
        "chatgpt": (
            "You are {name}, a staff engineer reviewing the same code. Focus on architecture, "
            "readability, and long-term maintainability. Challenge the other reviewer's "
            "priorities if you disagree. End with " + STOP_TOKEN + " only when nothing new remains."
        ),
        "conclusion_prompt": (
            "You have just finished a multi-round code review debate. "
            "Produce the FINAL REVIEW VERDICT and the fixed code.\n\n"
            "Structure EXACTLY:\n\n"
            "**Verdict:** SHIP | FIX FIRST | REWRITE\n\n"
            "**Blocking issues:**\n"
            "- [file:line] issue → fix\n"
            "- ...\n\n"
            "**Non-blocking issues:**\n"
            "- ...\n\n"
            "**FINAL CODE (all fixed files):**\n"
            "Output the complete corrected version of every file that changed. "
            "Complete contents, not diffs, not summaries.\n\n"
            "**Next action:** (what to run/test first)\n\n"
            "Do NOT re-explain the code. Just the verdict, the issues, and the fixed files."
        ),
    },
    "Business idea stress-test": {
        "deepseek": (
            "You are {name}. Analyze this business idea from a unit-economics and market lens. "
            "Ask about CAC, LTV, margins, and competition. Be skeptical of optimism. "
            "End with " + STOP_TOKEN + " only when nothing new remains."
        ),
        "chatgpt": (
            "You are {name}. Analyze from operations, regulatory, and founder-fit angle. "
            "Explicitly contradict the other AI if they're glossing over execution risk. "
            "End with " + STOP_TOKEN + " only when nothing new remains."
        ),
        "conclusion_prompt": (
            "You have just finished a business stress-test debate. "
            "Produce the FINAL VERDICT.\n\n"
            "Structure EXACTLY:\n\n"
            "**Verdict:** viable | viable with changes | not viable\n\n"
            "**Fatal flaw (if any):** (one line, or 'none')\n\n"
            "**Unit economics:**\n"
            "- Price: ...\n"
            "- CAC (assumption): ...\n"
            "- Margin: ...\n"
            "- Break-even: ...\n\n"
            "**Strongest objection:** (one line)\n\n"
            "**Recommended pivot (if needed):** (one paragraph)\n\n"
            "**Next action:** (validate or kill — one concrete step)\n\n"
            "Be decisive. No hedging."
        ),
    },
    "Explain like I'm 5": {
        "deepseek": (
            "You are {name}. Explain concepts using analogies and simple language. "
            "Build on the other AI's explanation, don't repeat it. "
            "End with " + STOP_TOKEN + " only when nothing new remains."
        ),
        "chatgpt": (
            "You are {name}. Add one deeper layer the other AI missed — a nuance, counterexample, "
            "or common misconception. Keep it simple but don't dumb it down. "
            "End with " + STOP_TOKEN + " only when nothing new remains."
        ),
    },
}

st.sidebar.header("⚙️ Mode")
mode = st.sidebar.selectbox(
    "Who should respond?",
    ["Both (DeepSeek then ChatGPT)", "DeepSeek only", "ChatGPT only"],
)

preset_name = st.sidebar.selectbox("Preset", list(PRESETS.keys()))
preset = PRESETS[preset_name]

with st.sidebar.expander("Edit system prompts", expanded=False):
    prompt_deepseek = st.text_area("DeepSeek prompt", value=preset["deepseek"], height=200)
    prompt_chatgpt = st.text_area("ChatGPT prompt", value=preset["chatgpt"], height=200)

with st.sidebar.expander("Preview conclusion prompt", expanded=False):
    st.caption(
        "This is what the conclusion AI will be told when silent mode produces "
        "the final answer. Falls back to a generic deliverable prompt if the "
        "current preset doesn't define one."
    )
    st.code(preset.get("conclusion_prompt", DEFAULT_CONCLUSION_PROMPT), language="markdown")

max_tokens = st.sidebar.slider("Max tokens per debate reply", 100, 2000, 500, 50)
conclusion_tokens = st.sidebar.slider("Max tokens for final conclusion", 500, 8000, 4000, 500)
history_limit = st.sidebar.slider("History window (messages)", 4, 40, 12, 2)

st.sidebar.markdown("---")
st.sidebar.subheader("🤖 Auto-Debate")
rounds = st.sidebar.slider(
    "Deliberation rounds (after opening)",
    1, 35, 4,
    help="Opening round is always 1 per AI. This slider adds back-and-forth deliberation rounds after that.",
)

n_ais = 2 if mode == "Both (DeepSeek then ChatGPT)" else 1
est_calls = (1 + rounds) * n_ais
st.sidebar.caption(f"≈ {est_calls} API calls per message")

st.sidebar.markdown("---")
st.sidebar.subheader("🎯 Silent Debate")
silent_mode = st.sidebar.checkbox(
    "Hide debate — show only final conclusion",
    value=False,
    help="Both AIs still debate fully in the background. You only see the final agreed conclusion.",
)
conclusion_ai = st.sidebar.selectbox(
    "Who writes the final conclusion?",
    ["DeepSeek", "ChatGPT"],
    index=0,
    disabled=not silent_mode,
)

if st.button("🔄 Continue debate", use_container_width=True):
    if st.session_state.debate_ended:
        st.warning("Debate already ended. Send a new message to start fresh.")
    else:
        st.session_state.run_extra_rounds = rounds
        st.rerun()

if st.button("🆕 New chat", use_container_width=True):
    st.session_state.session_id = db_create_session()
    st.session_state.messages = []
    st.session_state.debate_ended = False
    st.session_state.judge_verdict = None
    st.rerun()

# ============================================================
# API clients
# ============================================================
@st.cache_resource
def get_openai_client(api_key):
    return OpenAI(api_key=api_key, base_url="https://api.openai.com/v1", timeout=60.0, max_retries=3)

@st.cache_resource
def get_deepseek_client(api_key):
    return OpenAI(api_key=api_key, base_url="https://api.deepseek.com", timeout=60.0, max_retries=3)

openai_client = get_openai_client(openai_api_key) if openai_api_key and mode != "DeepSeek only" else None
deepseek_client = get_deepseek_client(deepseek_api_key) if deepseek_api_key and mode != "ChatGPT only" else None

# ============================================================
# UI Tabs
# ============================================================
tab_chat, tab_judge, tab_sessions, tab_info = st.tabs(["💬 Chat", "⚖️ Judge", "📚 Sessions", "⚙️ Info"])

# ---------- Chat tab ----------
with tab_chat:
    st.title("Multi-AI Roundtable")

    for msg in st.session_state.messages:
        if silent_mode and msg.get("role") == "assistant":
            continue
        if msg.get("role") == "conclusion":
            with st.container(border=True):
                st.markdown("### 🎯 Final Conclusion")
                st.markdown(msg["content"])
        else:
            with st.chat_message(msg["name"]):
                st.markdown(msg["content"])

    def get_prompt_for(name):
        return prompt_deepseek if name == "DeepSeek" else prompt_chatgpt

    def build_messages(current_model_name, turn_mode="deliberation"):
        trimmed = st.session_state.messages[-history_limit:]
        system_text = get_prompt_for(current_model_name).format(name=current_model_name)

        if turn_mode == "opening":
            system_text += (
                "\n\n=== THIS TURN: OPENING STATEMENT ===\n"
                "This is your opening. Speak INDEPENDENTLY — give your own best take on the "
                "user's prompt. Do NOT reference, agree with, or respond to the other AI yet. "
                "You have not seen their reply. State your position clearly and concretely."
            )
            last_user_idx = -1
            for i, m in enumerate(trimmed):
                if m["role"] == "user":
                    last_user_idx = i
            relevant = trimmed[: last_user_idx + 1] if last_user_idx >= 0 else trimmed
        else:
            system_text += (
                "\n\n=== THIS TURN: DELIBERATION ===\n"
                "You have now seen the other AI's opening and replies. Your job is to CONVERGE. Specifically:\n"
                "1. Acknowledge at least one point where the other AI is right (one line).\n"
                "2. Identify your strongest remaining disagreement and resolve it with concrete reasoning.\n"
                "3. Propose a unified position that combines both views where possible.\n"
                "4. If you have nothing new to add, end with " + STOP_TOKEN + ".\n"
                "Do not simply restate your opening. Move toward a shared conclusion."
            )
            relevant = trimmed

        out = [{"role": "system", "content": system_text}]
        for m in relevant:
            if m["role"] == "user":
                out.append({"role": "user", "content": f"User: {m['content']}"})
            elif m["role"] == "conclusion":
                out.append({"role": "user", "content": f"[Joint Conclusion]: {m['content']}"})
            else:
                if m["name"] == current_model_name:
                    out.append({"role": "assistant", "content": m["content"]})
                else:
                    out.append({"role": "user", "content": f"[{m['name']}]: {m['content']}"})
        return out

    def stream_reply(client, model_name, ai_name, turn_mode="deliberation"):
        try:
            stream = client.chat.completions.create(
                model=model_name,
                messages=build_messages(ai_name, turn_mode),
                max_tokens=max_tokens,
                temperature=0.85,
                stream=True,
            )
            collected = []
            for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    collected.append(delta)
                    yield delta
            yield ("__DONE__", "".join(collected))
        except Exception as e:
            yield ("__ERROR__", f"⚠️ {ai_name} error: {e}")

    def take_turn(ai_name, turn_mode="deliberation", silent=False):
        client = deepseek_client if ai_name == "DeepSeek" else openai_client
        model = "deepseek-chat" if ai_name == "DeepSeek" else "gpt-4o-mini"

        if not client:
            return False

        label = "opening" if turn_mode == "opening" else "response"
        full = ""
        final = None
        err = None

        if silent:
            for chunk in stream_reply(client, model, ai_name, turn_mode):
                if isinstance(chunk, tuple):
                    tag, val = chunk
                    if tag == "__DONE__":
                        final = val
                    elif tag == "__ERROR__":
                        err = val
                    break
                full += chunk
        else:
            with st.chat_message(ai_name):
                status = st.empty()
                status.markdown(f"⏳ *{ai_name} is preparing its {label}...*")

                placeholder = st.empty()
                first_chunk_seen = False
                last_paint = time.time()

                for chunk in stream_reply(client, model, ai_name, turn_mode):
                    if isinstance(chunk, tuple):
                        tag, val = chunk
                        if tag == "__DONE__":
                            final = val
                        elif tag == "__ERROR__":
                            err = val
                        break

                    if not first_chunk_seen:
                        status.markdown(f"✍️ *{ai_name} is writing...*")
                        first_chunk_seen = True

                    full += chunk
                    if time.time() - last_paint > 0.08:
                        placeholder.markdown(full + "▌")
                        last_paint = time.time()

                placeholder.markdown(full)

            status.empty()

        if err:
            if not silent:
                placeholder.markdown(err)
            st.session_state.messages.append({"role": "assistant", "name": ai_name, "content": err})
            db_add_message(st.session_state.session_id, "assistant", ai_name, err)
            return False

        final = final or full
        stopped = STOP_TOKEN in final
        clean = final.replace(STOP_TOKEN, "").strip()

        if not silent:
            placeholder.markdown(clean)
            if stopped:
                st.caption("🛑 *Signaled: nothing more to add*")

        st.session_state.messages.append({"role": "assistant", "name": ai_name, "content": clean})
        db_add_message(st.session_state.session_id, "assistant", ai_name, clean)

        st.session_state.usage["calls"] += 1
        return stopped

    def active_ais():
        out = []
        if deepseek_client and mode in ["Both (DeepSeek then ChatGPT)", "DeepSeek only"]:
            out.append("DeepSeek")
        if openai_client and mode in ["Both (DeepSeek then ChatGPT)", "ChatGPT only"]:
            out.append("ChatGPT")
        return out

    def run_debate(initial_round=True, extra_rounds=0, silent=False):
        ended = st.session_state.debate_ended
        if ended:
            return True
        ais = active_ais()

        total_turns = (len(ais) if initial_round else 0) + extra_rounds * len(ais)
        done_turns = 0
        progress = None
        if silent and total_turns > 0:
            progress = st.progress(0, text=f"🎯 Silent debate in progress (0/{total_turns})...")

        if initial_round:
            for ai in ais:
                if ended:
                    break
                ended = take_turn(ai, turn_mode="opening", silent=silent)
                if silent and progress:
                    done_turns += 1
                    progress.progress(done_turns / total_turns,
                                      text=f"🎯 Silent debate ({done_turns}/{total_turns})...")
                else:
                    st.divider()
                    time.sleep(1.5)

        for _ in range(extra_rounds):
            if ended:
                break
            for ai in ais:
                if ended:
                    break
                ended = take_turn(ai, turn_mode="deliberation", silent=silent)
                if silent and progress:
                    done_turns += 1
                    progress.progress(done_turns / total_turns,
                                      text=f"🎯 Silent debate ({done_turns}/{total_turns})...")
                else:
                    st.divider()
                    time.sleep(1.5)

        if progress:
            progress.empty()

        st.session_state.debate_ended = ended
        return ended

    def generate_conclusion(ai_name):
        client = deepseek_client if ai_name == "DeepSeek" else openai_client
        model = "deepseek-chat" if ai_name == "DeepSeek" else "gpt-4o-mini"
        if not client:
            return None

        transcript = "\n\n".join(
            f"[{m['name']}]: {m['content']}"
            for m in st.session_state.messages
            if m.get("role") != "conclusion"
        )

        # Preset-specific conclusion prompt, with generic fallback
        system_prompt = preset.get("conclusion_prompt", DEFAULT_CONCLUSION_PROMPT)

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Debate transcript:\n\n{transcript}"},
                ],
                max_tokens=conclusion_tokens,
                temperature=0.3,
            )
            return resp.choices[0].message.content
        except Exception as e:
            return f"⚠️ Conclusion error: {e}"

    def save_conclusion():
        st.session_state.messages = [
            m for m in st.session_state.messages if m.get("role") != "conclusion"
        ]
        has_ai = any(m.get("role") == "assistant" for m in st.session_state.messages)
        if not has_ai:
            return
        with st.spinner(f"🎯 {conclusion_ai} is writing the final conclusion..."):
            text = generate_conclusion(conclusion_ai)
        if text:
            st.session_state.messages.append({
                "role": "conclusion",
                "name": "🎯 Conclusion",
                "content": text,
            })
            db_add_message(st.session_state.session_id, "conclusion", "🎯 Conclusion", text)

    if st.session_state.run_extra_rounds > 0 and not st.session_state.debate_ended:
        n = st.session_state.run_extra_rounds
        st.session_state.run_extra_rounds = 0
        ended = run_debate(initial_round=False, extra_rounds=n, silent=silent_mode)
        if silent_mode:
            save_conclusion()
        elif ended:
            st.info("🛑 Debate ended.")
        st.rerun()

    if prompt := st.chat_input("Type your message..."):
        st.session_state.debate_ended = False
        st.session_state.judge_verdict = None

        st.session_state.messages.append({"role": "user", "name": "You", "content": prompt})
        db_add_message(st.session_state.session_id, "user", "You", prompt)
        with st.chat_message("You"):
            st.markdown(prompt)

        if len([m for m in st.session_state.messages if m["role"] == "user"]) == 1:
            db_rename_session(st.session_state.session_id, prompt[:50])

        ended = run_debate(initial_round=True, extra_rounds=rounds, silent=silent_mode)

        if silent_mode:
            save_conclusion()
        elif not ended:
            st.caption("⏱️ Max rounds reached. Click **Continue debate** to keep going.")
        st.rerun()

# ---------- Judge tab ----------
with tab_judge:
    st.title("⚖️ Judge Verdict")
    st.caption("A third AI reads the full transcript and scores the debate.")

    if not openai_client and not deepseek_client:
        st.warning("Add at least one API key to use the judge.")
    else:
        judge_provider = st.radio("Judge model", ["DeepSeek", "ChatGPT"], horizontal=True)
        if st.button("🧑‍⚖️ Judge this debate"):
            judge_client = deepseek_client if judge_provider == "DeepSeek" else openai_client
            judge_model = JUDGE_MODEL_DEEPSEEK if judge_provider == "DeepSeek" else JUDGE_MODEL_OPENAI

            if not judge_client:
                st.error(f"No {judge_provider} client available.")
            else:
                transcript = "\n\n".join(
                    f"[{m['name']}]: {m['content']}" for m in st.session_state.messages
                )
                judge_system = (
                    "You are an impartial judge evaluating a debate between AI assistants. "
                    "You will produce a structured verdict with these sections:\n"
                    "1. **Winner** (DeepSeek, ChatGPT, or Draw) — one line.\n"
                    "2. **Why** — 2–3 sentences.\n"
                    "3. **Strongest point from each side** — one bullet each.\n"
                    "4. **Weakest point from each side** — one bullet each.\n"
                    "5. **What neither side addressed** — one bullet.\n"
                    "Be blunt. Do not hedge."
                )
                with st.spinner("Judge is deliberating..."):
                    try:
                        resp = judge_client.chat.completions.create(
                            model=judge_model,
                            messages=[
                                {"role": "system", "content": judge_system},
                                {"role": "user", "content": f"Debate transcript:\n\n{transcript}"},
                            ],
                            max_tokens=800,
                            temperature=0.4,
                        )
                        verdict = resp.choices[0].message.content
                        st.session_state.judge_verdict = verdict
                    except Exception as e:
                        st.error(f"Judge error: {e}")

        if st.session_state.judge_verdict:
            st.markdown(st.session_state.judge_verdict)

# ---------- Sessions tab ----------
with tab_sessions:
    st.title("📚 Saved Sessions")

    st.caption(
        f"Currently loaded: **session #{st.session_state.session_id}** "
        f"with **{len(st.session_state.messages)}** messages in memory."
    )

    sessions = db_list_sessions()
    if not sessions:
        st.caption("No saved sessions yet.")
    for sid, title, updated in sessions:
        col1, col2, col3, col4 = st.columns([5, 1, 1, 1])
        with col1:
            marker = " 🟢" if sid == st.session_state.session_id else ""
            st.markdown(
                f"**{title or 'Untitled'}**{marker}  \n<small>#{sid} · {updated}</small>",
                unsafe_allow_html=True,
            )
        with col2:
            if st.button("Load", key=f"load_{sid}"):
                loaded = db_load_messages(sid)
                st.session_state.session_id = sid
                st.session_state.messages = loaded
                st.session_state.debate_ended = False
                st.session_state.judge_verdict = None
                st.session_state["_load_flash"] = f"✅ Loaded session #{sid} with {len(loaded)} messages."
                st.rerun()
        with col3:
            if st.button("Check", key=f"chk_{sid}"):
                n = len(db_load_messages(sid))
                st.info(f"Session #{sid} contains {n} messages in DB.")
        with col4:
            if st.button("Delete", key=f"del_{sid}"):
                db_delete_session(sid)
                st.rerun()

    if st.session_state.get("_load_flash"):
        st.success(st.session_state["_load_flash"])
        st.session_state["_load_flash"] = None

# ---------- Info tab ----------
with tab_info:
    st.title("ℹ️ Session Info")

    st.subheader("📦 Export ALL sessions")
    st.caption("Downloads every session in the database — not just the currently loaded one.")

    sessions_all = db_list_sessions()
    if not sessions_all:
        st.caption("No sessions in the database yet.")
    else:
        st.caption(f"Total sessions in DB: **{len(sessions_all)}**")
        col1, col2 = st.columns(2)
        with col1:
            st.download_button(
                "⬇️ Download ALL as Markdown",
                data=db_export_all_markdown(),
                file_name=f"roundtable_ALL_{dt.datetime.now().strftime('%Y%m%d_%H%M')}.md",
                mime="text/markdown",
                use_container_width=True,
            )
        with col2:
            st.download_button(
                "⬇️ Download ALL as JSON",
                data=db_export_all_json(),
                file_name=f"roundtable_ALL_{dt.datetime.now().strftime('%Y%m%d_%H%M')}.json",
                mime="application/json",
                use_container_width=True,
            )

    st.markdown("---")

    st.subheader("Usage this session")
    u = st.session_state.usage
    c1, c2, c3 = st.columns(3)
    c1.metric("API calls", u["calls"])
    c2.metric("~ input tokens", u["in_tokens"])
    c3.metric("~ output tokens", u["out_tokens"])
    st.caption("Token counts are approximate. Check your provider dashboard for exact billing.")

    st.subheader("Export transcript")
    if st.session_state.messages:
        md = "# Multi-AI Roundtable Transcript\n\n"
        md += f"Exported: {dt.datetime.now().isoformat()}\n\n---\n\n"
        for m in st.session_state.messages:
            md += f"### {m['name']}\n\n{m['content']}\n\n"
        if st.session_state.judge_verdict:
            md += "---\n\n## Judge Verdict\n\n" + st.session_state.judge_verdict + "\n"
        st.download_button(
            "⬇️ Download as Markdown",
            data=md,
            file_name=f"roundtable_{st.session_state.session_id}.md",
            mime="text/markdown",
        )
        st.download_button(
            "⬇️ Download as JSON",
            data=json.dumps(st.session_state.messages, indent=2),
            file_name=f"roundtable_{st.session_state.session_id}.json",
            mime="application/json",
        )
    else:
        st.caption("No messages yet.")

    st.subheader("Current preset")
    st.code(preset_name)

    st.subheader("How the debate stops")
    st.markdown(
        f"- An AI emits `{STOP_TOKEN}` when it has nothing new to add.\n"
        "- The token is stripped from the displayed message and the loop breaks.\n"
        "- The **Continue debate** button won't revive an ended debate — send a new message instead."
    )