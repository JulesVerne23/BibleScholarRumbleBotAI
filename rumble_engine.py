# =============================================================================
# rumble_engine.py
# BibleScholar — Rumble Bot engine
#
# Fully independent from scholar_engine.py.  Uses its own Ollama model
# (llama3:latest), its own lock, and its own thread — so it NEVER blocks
# the Scholar UI and the Scholar UI never blocks it.
#
# Imported by BibleScholarRumble64.py (UI only).
# =============================================================================

import re
import json
import time
import threading
import requests
from queue import Queue
from collections import OrderedDict

# scholar_engine is imported for shared helpers (verse lookup, Zotero,
# Strong's injection, book normalization, activity log, sanitize utils).
# No circular import — scholar_engine imports nothing from here.
import scholar_engine as se

# =============================================================================
# RUMBLE-SPECIFIC CONFIGURATION
# =============================================================================
RUMBLE_MODEL        = "llama3:latest"          # fast model for chat replies
RUMBLE_OLLAMA_URL   = se.OLLAMA_URL            # same endpoint, different model
RUMBLE_POLL_INTERVAL = 4                       # seconds between chat polls
RUMBLE_CHAR_LIMIT   = 780                      # total chars across all messages
RUMBLE_ENTRY_CHARS  = 200                      # chars per individual chat message
MAX_POLL_ERRORS     = 10                       # consecutive errors before auto-disconnect

# Reduced context window for speed — llama3 on Rumble questions doesn't need
# the full 8192.  4096 comfortably fits: system prompt (~300 tok) +
# Zotero block (~300 tok) + question (~50 tok) + full reply (~1100 tok).
RUMBLE_NUM_CTX      = 4096

# Ollama import guard — Selenium only needed at runtime
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from webdriver_manager.chrome import ChromeDriverManager
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

# =============================================================================
# INDEPENDENT OLLAMA LOCK
# The Rumble bot acquires this lock — completely separate from
# scholar_engine.scholar_ollama_lock — so UI and bot never queue behind each other.
# =============================================================================
_rumble_ollama_lock = threading.Lock()

# =============================================================================
# RUMBLE STATE  — shared between the bot thread and the UI thread
# =============================================================================
rumble_state: dict = {
    "driver":    None,
    "running":   False,
    "thread":    None,
    "seen_ids":  OrderedDict(),   # insertion-ordered; oldest evicted first
    "log_queue": Queue(),         # thread-safe log lines for the UI widget
}

# UI callbacks — set by BibleScholarRumble64.py after widgets are created
# so the engine never imports tkinter directly.
_ui_callbacks: dict = {
    "set_status":        None,   # fn(text: str)
    "update_viewer":     None,   # fn(text: str)
    "enable_connect":    None,   # fn()
    "disable_connect":   None,   # fn()
    "enable_disconnect": None,   # fn()
    "disable_disconnect":None,   # fn()
    "enable_stop":       None,   # fn()
    "disable_stop":      None,   # fn()
}

def register_ui_callbacks(**kwargs):
    """Call once from BibleScholarRumble64.py to wire UI update functions."""
    _ui_callbacks.update(kwargs)

def _ui(name, *args):
    fn = _ui_callbacks.get(name)
    if fn:
        fn(*args)

# =============================================================================
# LOGGING
# =============================================================================
def rumble_log(msg: str):
    """Thread-safe: push a line into the log queue for the UI to display."""
    rumble_state["log_queue"].put(msg)

# =============================================================================
# CHAR-LIMIT HELPERS
# =============================================================================
def _calc_max_parts() -> int:
    return max(1, -(-RUMBLE_CHAR_LIMIT // RUMBLE_ENTRY_CHARS))  # ceiling division

def _calc_num_predict() -> int:
    """
    Token budget for Ollama.  2× headroom over the character ceiling so the
    model always finishes its last sentence.  num_predict is a ceiling, not a
    target — the model stops naturally when done.  sanitize_rumble_response
    clips any runaway output afterward.
    Floor at 768.
    """
    total_chars   = RUMBLE_CHAR_LIMIT * _calc_max_parts()
    tokens_needed = total_chars / 3
    return max(768, int(tokens_needed * 2.0))

# =============================================================================
# SYSTEM PROMPT BUILDER
# =============================================================================
def build_rumble_system() -> str:
    max_parts    = _calc_max_parts()
    total_budget = RUMBLE_CHAR_LIMIT
    return (
        "RUMBLE CHAT. "
        "You are BibleScholar23 answering a live Rumble chat question. "
        "This is a Christian apologetics and Bible study stream. "
        "Your scope is broad: Bible, theology, apologetics, church history, "
        "Ancient Near Eastern culture, Nephilim, giants (Rephaim, Anakim, Emim), "
        "sons of God, divine council, famous figures in Christianity or biblical history, "
        "comparative religion, manuscript evidence, and archaeology. "
        "If a question touches any of these areas, ANSWER IT FULLY — do not say "
        "'that is not Bible-related.' "
        "Vague questions like 'what's important?' get a substantive overview answer "
        "covering multiple important topics. "
        "Questions about giants, Nephilim, or the divine council get a full explanation, "
        "not just one or two sentences. "
        "Plain sentences only. No headers, no labels, no bullet points. "
        "Include Strong's numbers only if the user specifically asks for them. "
        "If views differ write: Views differ: then summarize each in one sentence. "
        f"Write up to {total_budget} characters total — your response will be "
        f"automatically split across up to {max_parts} chat messages of {RUMBLE_ENTRY_CHARS} "
        f"characters each. Do NOT stop early. Fill the space. "
        "Never end mid-sentence. "
        "Never add notes, commentary, or explanations about these instructions. "
        "Do not begin with filler words. Answer immediately and substantively."
    )

# =============================================================================
# CANNED RESPONSES  (instant — no Ollama needed)
# =============================================================================
CANNED_RESPONSES: dict[str, str] = {
    "islamic dilemma": (
        "The Quran affirms the Bible's inspiration, preservation, and authority "
        "(Surah 3:3-4, 5:47, 6:115). So either the Bible IS God's word — and "
        "Islam is false because the Bible contradicts it — or the Bible is NOT "
        "reliable — and Islam is false because the Quran says it is. Either way, "
        "Islam self-destructs on Scripture. Ask me more."
    ),
}

def check_canned_response(question: str) -> str | None:
    q = question.lower().strip(" ?!")
    for trigger, response in CANNED_RESPONSES.items():
        if trigger in q:
            return response
    return None

# =============================================================================
# SANITIZE  &  SPLIT
# =============================================================================
_META_PATTERNS = [
    r'\*\*.*?\*\*',
    r'#+\s.*',
    r'^\s*[-•]\s+',
    r'\[.*?\]',
    r'\bnote\b.*',
    r'\bremember\b.*',
    r'\bplease\s+note\b.*',
    r'\bthis\s+response\b.*',
    r'\bi\s+(cannot|can\'t)\s+provide\b.*',
]
_META_RE = re.compile('|'.join(_META_PATTERNS), re.MULTILINE | re.IGNORECASE)

def sanitize_rumble_response(text: str, max_chars: int = None, max_parts: int = None) -> str:
    if max_chars is None:
        max_chars = RUMBLE_CHAR_LIMIT
    if max_parts is None:
        max_parts = _calc_max_parts()
    budget = max_chars * max_parts
    text = se.strip_non_latin(text)
    text = _META_RE.sub('', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) <= budget:
        return text
    truncated = text[:budget]
    last_end = max(
        truncated.rfind('.'), truncated.rfind('!'), truncated.rfind('?')
    )
    if last_end > budget // 2:
        text = truncated[:last_end + 1].strip()
    else:
        text = truncated.rstrip()
    return text

def split_into_chat_entries(text: str, max_chars: int = None, max_parts: int = None) -> list[str]:
    if max_chars is None:
        max_chars = RUMBLE_ENTRY_CHARS
    if max_parts is None:
        max_parts = _calc_max_parts()
    if len(text) <= max_chars:
        return [text]
    parts, remaining = [], text
    while remaining and len(parts) < max_parts:
        if len(remaining) <= max_chars:
            parts.append(remaining)
            break
        chunk = remaining[:max_chars]
        last_space = chunk.rfind(' ')
        if last_space > max_chars // 2:
            cut = last_space
        else:
            cut = max_chars
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    return parts

# =============================================================================
# OLLAMA CALL  — non-streaming, independent lock
# =============================================================================
def get_ollama_response(user_message: str, zotero_block: str = "") -> str | None:
    """
    Non-streaming Ollama call using RUMBLE_MODEL (llama3:latest).
    Acquires _rumble_ollama_lock independently from the Scholar UI lock,
    so the two models run concurrently without waiting on each other.
    """
    grounded = se.inject_strongs_facts(user_message)
    if zotero_block:
        grounded = f"{zotero_block}\n\n{grounded}"

    full_prompt = f"{build_rumble_system()}\n\n{grounded}"
    payload = {
        "model":   RUMBLE_MODEL,
        "messages": [{"role": "user", "content": full_prompt}],
        "stream":  False,
        "options": {
            "num_ctx":     RUMBLE_NUM_CTX,
            "num_predict": _calc_num_predict(),
        },
    }

    rumble_log("Querying AI (llama3)…")
    acquired = _rumble_ollama_lock.acquire(timeout=300)
    if not acquired:
        rumble_log("[Ollama timeout] Could not acquire lock after 300s.")
        return None
    try:
        r = requests.post(RUMBLE_OLLAMA_URL, json=payload, timeout=300)
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "").strip()
    except requests.exceptions.HTTPError as e:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        rumble_log(f"[Ollama HTTP error] {e} | {detail}")
        return None
    except Exception as e:
        rumble_log(f"[Ollama error] {e}")
        return None
    finally:
        _rumble_ollama_lock.release()

# =============================================================================
# ZOTERO  — async pre-fetch so it runs in parallel with Ollama startup
# =============================================================================
def fetch_zotero_async(question: str, result_holder: list):
    """
    Fetch Zotero context in a background thread.
    result_holder is a single-item list; set result_holder[0] when done.
    The bot loop waits up to 8 seconds for the result before proceeding.
    """
    result_holder[0] = se.zotero_context_for(question)

# =============================================================================
# SELENIUM HELPERS
# =============================================================================
def rumble_login(driver, email: str, password: str) -> bool:
    try:
        rumble_log("Navigating to Rumble login…")
        driver.get("https://rumble.com/login")
        wait = WebDriverWait(driver, 20)
        email_field = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR,
             "input[type='email'], input[name='email'], "
             "input[name='username'], input[type='text']")
        ))
        email_field.clear()
        email_field.send_keys(email)
        pass_field = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
        pass_field.clear()
        pass_field.send_keys(password)
        pass_field.send_keys(Keys.RETURN)
        wait.until(EC.url_changes("https://rumble.com/login"))
        time.sleep(2)
        rumble_log(f"Logged in as {email}.")
        return True
    except Exception as e:
        rumble_log(f"[Login error] {e}")
        return False

def rumble_navigate_to_stream(driver, stream_url: str):
    rumble_log(f"Opening stream: {stream_url}")
    driver.get(stream_url)
    time.sleep(4)

def get_chat_messages(driver) -> list[dict]:
    messages = []
    try:
        items = driver.find_elements(
            By.CSS_SELECTOR,
            "ul.chat-history--list li, .rumbles-vote li, .chat-history li"
        )
        items = items[-20:]
        for item in items:
            try:
                author_el = item.find_element(
                    By.CSS_SELECTOR, ".chat-username, .username, [class*='username']")
                text_el = item.find_element(
                    By.CSS_SELECTOR, ".chat-message--body, .message-body, [class*='message']")
                author = author_el.text.strip()
                text   = text_el.text.strip()
                uid    = f"{author}::{text}"
                messages.append({"id": uid, "author": author, "text": text})
            except Exception:
                pass
    except Exception as e:
        rumble_log(f"[Chat scrape error] {e}")
    return messages

def post_chat_reply(driver, reply_text: str, max_retries: int = 3) -> bool:
    reply_text = se.strip_non_latin(reply_text)
    for attempt in range(max_retries):
        try:
            wait = WebDriverWait(driver, 10)
            chat_input = wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR,
                 "textarea.chat-input, input.chat-input, [placeholder*='chat'], "
                 "[class*='chat-input'], textarea[name='message']")
            ))
            chat_input.click()
            chat_input.send_keys(Keys.CONTROL, 'a')
            chat_input.send_keys(Keys.DELETE)
            try:
                import pyperclip
                pyperclip.copy(reply_text)
                chat_input.send_keys(Keys.CONTROL, 'v')
                time.sleep(0.2)
            except ImportError:
                chat_input.send_keys(reply_text)
            actual = (
                chat_input.get_attribute("value")
                or chat_input.get_attribute("innerText")
                or driver.execute_script("return arguments[0].innerText;", chat_input)
                or ""
            )
            if actual and se.strip_non_latin(actual) != actual.strip():
                chat_input.send_keys(Keys.CONTROL, 'a')
                chat_input.send_keys(Keys.DELETE)
                raise RuntimeError("CJK contamination — retrying")
            chat_input.send_keys(Keys.RETURN)
            time.sleep(0.5)
            return True
        except Exception as e:
            wait_secs = 2 ** attempt
            rumble_log(f"[Post reply error, attempt {attempt+1}/{max_retries}] {e} — retrying in {wait_secs}s")
            time.sleep(wait_secs)
    rumble_log("[Post reply failed] Gave up after all retries.")
    return False

# =============================================================================
# MAIN BOT LOOP
# =============================================================================
def rumble_bot_loop(stream_url: str, username: str, password: str, bot_name: str):
    if not SELENIUM_AVAILABLE:
        rumble_log("[Error] selenium / webdriver-manager not installed.")
        rumble_log("Run:  pip install selenium webdriver-manager")
        _ui("set_status", "Error — missing libraries")
        return

    rumble_log("Launching Chrome…")
    opts = Options()
    opts.add_argument("--start-maximized")

    try:
        service = Service(ChromeDriverManager().install())
        driver  = webdriver.Chrome(service=service, options=opts)
        rumble_state["driver"] = driver
    except Exception as e:
        rumble_log(f"[Chrome launch error] {e}")
        _ui("set_status", "Error — Chrome failed")
        return

    if not rumble_login(driver, username, password):
        _ui("set_status", "Error — login failed")
        driver.quit()
        return

    rumble_navigate_to_stream(driver, stream_url)
    _ui("set_status", "Connected — watching chat…")

    mention_re        = re.compile(rf'@{re.escape(bot_name)}\b', re.IGNORECASE)
    consecutive_errors = 0

    while rumble_state["running"]:
        try:
            messages = get_chat_messages(driver)
            consecutive_errors = 0

            for msg in messages:
                uid = msg["id"]
                if uid in rumble_state["seen_ids"]:
                    continue
                # Record as seen — OrderedDict guarantees oldest is evicted first
                rumble_state["seen_ids"][uid] = None
                while len(rumble_state["seen_ids"]) > 500:
                    rumble_state["seen_ids"].popitem(last=False)

                if not mention_re.search(msg["text"]):
                    continue

                author   = msg["author"]
                question = mention_re.sub("", msg["text"]).strip()
                question = re.sub(r'^\S+\s*\n\s*', '', question).strip(" ,")
                rumble_log(f"@mention from {author}: {question}")

                # Prompt injection check
                is_suspicious = se.write_activity_log("MENTION", author, question)
                if is_suspicious:
                    rumble_log(f"[BLOCKED] Prompt injection from {author}.")
                    block_reply = f"@{author} I can't help with that."
                    post_chat_reply(driver, block_reply)
                    se.write_activity_log("BLOCKED", author, question, block_reply)
                    continue

                # ── Verse-only shortcut ──────────────────────────────────────
                if re.search(r'\bverse\s+only\b', question, re.IGNORECASE):
                    ref = re.sub(r'\bverse\s+only\b', '', question, flags=re.IGNORECASE).strip(" ,")
                    rumble_log(f"[Verse-only] ref after strip: '{ref}'")
                    verse_text = se.resolve_verse_from_net(ref)
                    if verse_text:
                        rumble_log("[Verse-only] NET hit — posting directly.")
                        reply = f"@{author} {verse_text}"
                        parts = split_into_chat_entries(reply)
                        se.write_activity_log("RESPONSE", author, question, " | ".join(parts))
                        for i, part in enumerate(parts):
                            post_chat_reply(driver, part)
                            if i < len(parts) - 1:
                                time.sleep(1.0)
                    else:
                        rumble_log(f"[Verse-only] NET miss — asking llama3 to quote verbatim.")
                        verse_prompt = (
                            f"Quote the NET Bible (New English Translation) text of {ref} "
                            "word for word, exactly as written. "
                            "Give only the reference and the verse text — nothing else. "
                            "No commentary, no explanation, no paraphrasing."
                        )
                        answer = get_ollama_response(verse_prompt)
                        if answer:
                            answer   = sanitize_rumble_response(answer)
                            prefixed = f"@{author} {answer}"
                            parts    = split_into_chat_entries(prefixed)
                            se.write_activity_log("RESPONSE", author, question, " | ".join(parts))
                            for i, part in enumerate(parts):
                                post_chat_reply(driver, part)
                                if i < len(parts) - 1:
                                    time.sleep(1.0)
                    continue

                # ── Canned responses ─────────────────────────────────────────
                canned = check_canned_response(question)
                if canned:
                    rumble_log(f"Serving canned response for '{question[:40]}'")
                    prefixed = f"@{author} {canned}"
                    parts    = split_into_chat_entries(prefixed)
                    se.write_activity_log("RESPONSE", author, question, " | ".join(parts))
                    for i, part in enumerate(parts):
                        post_chat_reply(driver, part)
                        if i < len(parts) - 1:
                            time.sleep(1.0)
                    continue

                # ── Async Zotero + Ollama  (run Zotero in parallel) ──────────
                zotero_result = [None]
                zotero_thread = threading.Thread(
                    target=fetch_zotero_async,
                    args=(question, zotero_result),
                    daemon=True
                )
                zotero_thread.start()

                # Give Zotero up to 8 seconds to finish before Ollama starts.
                # If it's not back in time we proceed without it — Ollama will
                # still answer, just without library citations.
                zotero_thread.join(timeout=8)
                zotero_block = zotero_result[0] or ""

                if zotero_block:
                    rumble_log(f"📚 Zotero: library sources found.")
                else:
                    rumble_log("📚 Zotero: no library sources found.")

                rumble_log("Querying llama3 for answer…")
                answer = get_ollama_response(
                    f"Rumble chat user {author} asks: {question}",
                    zotero_block=zotero_block,
                )

                if answer is None:
                    rumble_log(f"[Error] No response from llama3 for {author}'s question.")
                    se.write_activity_log("ERROR", author, question, "No response.")
                else:
                    answer   = sanitize_rumble_response(answer)
                    prefixed = f"@{author} {answer}"
                    parts    = split_into_chat_entries(prefixed)
                    full_log = " | ".join(parts)
                    rumble_log(f"Sending {len(parts)} message(s)…")
                    _ui("update_viewer", answer)
                    se.write_activity_log("RESPONSE", author, question, full_log)
                    for i, part in enumerate(parts):
                        rumble_log(f"  [{i+1}/{len(parts)}] {part[:60]}{'…' if len(part)>60 else ''}")
                        post_chat_reply(driver, part)
                        if i < len(parts) - 1:
                            time.sleep(1.0)

        except Exception as e:
            consecutive_errors += 1
            rumble_log(f"[Poll error #{consecutive_errors}] {e}")
            if consecutive_errors >= MAX_POLL_ERRORS:
                rumble_log(f"[Auto-disconnect] {MAX_POLL_ERRORS} consecutive errors — stopping.")
                rumble_state["running"] = False
                break

        time.sleep(RUMBLE_POLL_INTERVAL)

    rumble_log("Bot stopped.")
    try:
        driver.quit()
    except Exception:
        pass
    rumble_state["driver"] = None
    _ui("set_status", "Disconnected")
    _ui("enable_connect")
    _ui("disable_disconnect")
    _ui("disable_stop")

# =============================================================================
# CONNECT / DISCONNECT  (called from the UI thread)
# =============================================================================
def connect(stream_url: str, username: str, password: str, bot_name: str):
    if rumble_state["running"]:
        return
    rumble_state["running"] = True
    _ui("set_status", "Connecting…")
    _ui("disable_connect")
    _ui("enable_disconnect")
    _ui("enable_stop")
    t = threading.Thread(
        target=rumble_bot_loop,
        args=(stream_url, username, password, bot_name),
        daemon=True,
    )
    rumble_state["thread"] = t
    t.start()

def disconnect():
    rumble_state["running"] = False
    _ui("set_status", "Disconnecting…")
    _ui("enable_connect")
    _ui("disable_disconnect")
    _ui("disable_stop")